import Foundation
import Observation
import WristcallKit

enum LinkError: Error, Equatable {
    /// The code is not 8 digits (nothing was sent).
    case badCode
    /// The server linked the account and issued a personal token, but the app could not verify or save it.
    case notSaved
    /// A link URL arrived before the account was ready (nothing was sent).
    case accountNotReady
}

/// Restoring an account by email met an account on this iPhone that holds something.
enum RestoreError: Error, Equatable {
    /// The current account has `servers` servers (or a plan, or a sign-in): the screen asks before leaving it
    /// (`restoreWithEmail(leavingCurrent: true)`). It stays in the Cloud as it is.
    case currentAccountHasData(servers: Int)
}

/// The Cloud account on this iPhone (spec O5): silent, with no sign-in screen. The device session lives in
/// the Keychain and the account key in iCloud Keychain; a 0.3.x Zitadel sign-in is moved over once, then its
/// tokens go. Also: restoring an account by email, deleting it, redeeming invites, linking servers and keeping
/// the agenda in sync.
///
/// Optional: without a Cloud URL in the build (`WristcallCloudURL`) the state is `.unavailable` and the
/// account screens are hidden. Secrets (tokens, keys, codes) never reach a log or a message.
@MainActor
@Observable
final class AccountModel {
    enum State: Equatable {
        case unavailable, starting, ready
        /// The Cloud asked to wait (`429`) or could not be reached; `start` tries again by itself at `retryAt`.
        case waiting(retryAt: Date?)
        /// No session and nothing to sign in with: this iPhone had an account, so it never makes a new one
        /// on its own. The restore screen.
        case needsSignIn
        case failed(String)
    }

    static let callback = URL(string: "wristcall://auth/callback")!
    static let callbackScheme = "wristcall"
    /// The account this iPhone was last in, written on every `ready`. Set: a lost session never makes a new
    /// account, only signs in again with a stored key.
    static let lastIDKey = "account.lastID"
    /// `lastIDKey` when the account is not known: a 0.3.x Zitadel sign-in that died before it was moved over.
    static let unknownAccount = "unknown"

    private(set) var state: State
    /// `GET /v1/account`, once ready.
    private(set) var info: CloudAccountInfo?
    var plan: Plan? { info?.plan }
    /// Whether this iPhone holds a usable key of the current account (what "Sign out everywhere" needs).
    private(set) var hasLocalKey = false
    /// A 0.3.x account with its Zitadel sign-in: the only kind whose watch can still use the device page (R11).
    var isMigratedAccount: Bool { info?.identities.contains("zitadel") == true }
    /// The Zitadel sign-in of 0.3.x was moved over to a device session on this launch.
    private(set) var didMigrate = false
    /// The last failure of an action taken while ready (deleting the account), for the screen.
    private(set) var error: String?
    let cloudURL: URL?
    let appState: AppState
    /// The agenda mirror while an account is possible (`nil` when `.unavailable`). Suspended until ready.
    let agenda: AgendaSync?
    /// Runs after the Cloud deleted the account, before the local servers go: the app drops its push keys there.
    @ObservationIgnored var accountDeleted: (@MainActor () async -> Void)?
    /// The watch a linked server is set up on (`PhoneApp` sets it); `nil`: no watch step.
    @ObservationIgnored var watchLink: WatchLink?
    /// The watch set-up the last link started (it may wait for the watch's answer): tests wait for it.
    @ObservationIgnored private(set) var watchPairing: Task<WatchLink.SendResult, Never>?

    @ObservationIgnored private let session: DeviceSession?
    @ObservationIgnored private let keys: any AccountKeyStore
    /// The 0.3.x Zitadel sign-in: read to move it over, and the email restore's login. Nothing else uses it.
    @ObservationIgnored private let legacy: AccountSession?
    @ObservationIgnored private let web: any WebAuthenticator
    @ObservationIgnored private let device: CloudDeviceInfo
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let sleep: @Sendable (TimeInterval) async -> Void

    /// The start in flight: every `start()` waits for this one.
    @ObservationIgnored private var starting: Task<Void, Never>?
    /// The session ended while a start was running: it runs once more.
    @ObservationIgnored private var restartRequested = false
    /// An email restore is running: its Zitadel tokens are not leftovers.
    @ObservationIgnored private var restoring = false
    /// The watch's enroll code, reused while it has more than a minute left (C8).
    @ObservationIgnored private var watchCode: EnrollCode?
    /// The publishing of the enroll code the last `becameReady` started (tests wait for it).
    @ObservationIgnored private(set) var watchPublishing: Task<Void, Never>?

    /// Approving the watch's login (R11): the provider's device page, open or closed again.
    enum WatchApproval: Equatable {
        case idle
        /// The page is up (in the login's web session).
        case open
        /// The page was closed (by the user, or because the watch said it is signed in): the watch has the outcome.
        case done
        case failed(String)
    }

    /// Where an approval was started: the sheet the watch's code brought up, or Settings (typed code).
    enum WatchApprovalOrigin: Equatable {
        case sheet
        case settings
    }

    private(set) var watchApproval: WatchApproval = .idle
    /// The code whose device page is open.
    private(set) var approvingCode: String?
    /// Where the open page was started from (`nil` when none is open). Only `.sheet` keeps the sheet up.
    private(set) var watchApprovalOrigin: WatchApprovalOrigin?
    /// The last code approved from Settings: the sheet does not come up for it afterwards.
    private(set) var settingsApprovedCode: String?
    /// The device page's task; cancelling it closes the page (M13).
    @ObservationIgnored private var approvalTask: Task<(any Error)?, Never>?

    static let checkWatch = "Check your watch."
    static let badUserCode = "Enter the 8-character code the watch shows."

    init(
        cloudURL: URL?,
        session: DeviceSession?,
        keys: any AccountKeyStore,
        legacy: AccountSession?,
        web: any WebAuthenticator,
        state appState: AppState,
        device: CloudDeviceInfo,
        defaults: UserDefaults = .standard,
        sleep: @escaping @Sendable (TimeInterval) async -> Void = { try? await Task.sleep(for: .seconds($0)) }
    ) {
        let session = cloudURL == nil ? nil : session
        self.cloudURL = session == nil ? nil : cloudURL
        self.session = session
        self.keys = keys
        self.legacy = session == nil ? nil : legacy
        self.web = web
        self.appState = appState
        self.device = device
        self.defaults = defaults
        self.sleep = sleep
        self.state = session == nil ? .unavailable : .starting
        self.agenda = session.map { AgendaSync(session: $0, state: appState) }
        // Nothing is mirrored before there is an account: `ready` sends everything at once.
        agenda?.suspend()
        agenda?.onSignedOut = { [weak self] in self?.sessionEnded() }
        installHooks()
    }

    /// The build's Cloud URL (Info.plist `WristcallCloudURL`): `https://`, or `http://` on localhost only.
    /// Empty, unexpanded or anything else: no account.
    nonisolated static func cloudURL(fromInfoValue value: String?) -> URL? {
        guard let value, value.contains("://") else { return nil }
        return ServerAddress.parse(value)
    }

    // MARK: - Start

    /// Gets this iPhone into its account, without a screen. One run at a time: a call while one runs waits
    /// for it. In order: the stored session; the 0.3.x Zitadel sign-in (moved over, then its tokens go); a
    /// stored key when this iPhone had an account before (never a new account: `.needsSignIn` instead); a
    /// new account. `429` and network failures wait (`.waiting`) and try again by themselves.
    /// While an email restore runs it returns at once: the restore ends in its own state.
    func start() async {
        guard session != nil, !restoring else { return }
        await launchStart().value
    }

    /// Forgets the account this iPhone was in and starts again: with no key, a new account ("Start over").
    /// The old account stays in the Cloud.
    func startOver() async {
        guard session != nil, state != .ready, !restoring else { return }
        defaults.removeObject(forKey: Self.lastIDKey)
        state = .starting
        await start()
    }

    @discardableResult
    private func launchStart() -> Task<Void, Never> {
        if let starting { return starting }
        let sleep = sleep
        // Weak between the attempts: a model that is gone (a finished test) stops retrying.
        let task = Task { [weak self] in
            var restarts = 0
            loop: while true {
                switch await self?.attempt() ?? .done {
                case .done:
                    break loop
                case .again:
                    restarts += 1
                    if restarts >= 3 {
                        self?.state = .failed("Can't reach your account. Try again.")
                        break loop
                    }
                case .retry(let delay):
                    await sleep(delay)
                }
            }
            self?.starting = nil
        }
        starting = task
        return task
    }

    private enum Next { case done, again, retry(TimeInterval) }
    private enum Entry { case ready, needsSignIn }

    private func attempt() async -> Next {
        guard let session else { return .done }
        restartRequested = false
        // A re-check from `.needsSignIn` (back in the foreground) keeps the restore screen up meanwhile.
        if state != .ready, state != .needsSignIn { state = .starting }
        do {
            switch try await enter(session) {
            case .ready: await becameReady(session)
            case .needsSignIn: state = .needsSignIn
            }
        } catch {
            if let delay = Self.retryDelay(for: error) {
                state = .waiting(retryAt: Date().addingTimeInterval(delay))
                return .retry(delay)
            }
            state = .failed(Self.message(for: error))
        }
        return restartRequested ? .again : .done
    }

    private func enter(_ session: DeviceSession) async throws -> Entry {
        if await session.isSignedIn {
            // Zitadel tokens next to a session: a migration that stopped before it deleted them.
            if !restoring, let legacy, await legacy.isSignedIn { await legacy.signOut() }
            return .ready
        }
        if let legacy, await legacy.isSignedIn {
            do {
                // Always renewed: the Cloud wants a recent `iat` to move a Zitadel login to a device session.
                let zitadel = try await legacy.freshAccessToken()
                let tokens = try await bootstrap(session).migrate(zitadelAccessToken: zitadel)
                try await session.start(tokens)
                await legacy.signOut()
                didMigrate = true
                return .ready
            } catch AccountError.signedOut {
                // The Zitadel sign-in is dead: its account exists somewhere, so none is made here. A key that
                // came through iCloud Keychain still gets in below.
                if defaults.string(forKey: Self.lastIDKey) == nil {
                    defaults.set(Self.unknownAccount, forKey: Self.lastIDKey)
                }
            }
        }
        if defaults.string(forKey: Self.lastIDKey) != nil {
            guard let tokens = try await bootstrap(session).enrollWithStoredKeys() else { return .needsSignIn }
            try await session.start(tokens)
            return .ready
        }
        let (tokens, _) = try await bootstrap(session).run()
        try await session.start(tokens)
        return .ready
    }

    /// In the account: remembers it, reads it, takes an older synced key over an empty account, then mirrors
    /// the servers.
    private func becameReady(_ session: DeviceSession) async {
        guard let account = await session.accountID else { return }
        // A start on the way back to the foreground finds the account it was in: no new full mirror.
        var mirror = state != .ready
        if enteredAccount(account) { mirror = true }
        state = .ready
        let read = await readAccount()
        guard state == .ready else { return }
        // C6 only on what the Cloud said just now: an old `info` may miss servers added since.
        if read, await adoptOlderKey(session) {
            guard let account = await session.accountID else { return }
            _ = enteredAccount(account)
            mirror = true
            await refreshAccount()
            guard state == .ready else { return }
        }
        agenda?.resume()
        if mirror { agenda?.pushAll() }
        publishWatchCodeInBackground()
    }

    /// Writes the account as the last one. Another account than the last one: the agenda ids and links
    /// belonged to that one. `true` when the account is not the last one.
    private func enteredAccount(_ account: String) -> Bool {
        let previous = defaults.string(forKey: Self.lastIDKey)
        if let previous, previous != Self.unknownAccount, previous != account {
            appState.forgetAccount()
            watchCode = nil
        }
        defaults.set(account, forKey: Self.lastIDKey)
        return previous != account
    }

    /// C6: a key of another account arrived (iCloud Keychain was late) while this account holds nothing. The
    /// other account wins and the empty one goes (in the Cloud, with its own token, and its key). Only a key
    /// older than this account's own: two iPhones that raced to make an account settle on the same one.
    private func adoptOlderKey(_ session: DeviceSession) async -> Bool {
        guard let current = info, current.isEmpty else { return false }
        let stored = (try? keys.keys()) ?? []
        let own = stored.first { $0.account == current.account }
        let others = stored.filter { key in
            key.account != current.account && !DeadKeys.isDead(key, defaults: defaults)
                && (own.map { key.createdAt < $0.createdAt } ?? true)
        }
        let client = session.cloudClient
        for other in others {
            let tokens: SessionTokens
            do {
                tokens = try await client.enrollDevice(key: other.key, account: other.account, device: device)
            } catch APIError.unauthorized {
                DeadKeys.mark(other, defaults: defaults)
                continue
            } catch {
                return false
            }
            do {
                // Read again right before: a server added meanwhile (another device) keeps the account.
                let token = try await session.accessToken()
                let now = try await client.account(accessToken: token)
                guard now.account == current.account, now.isEmpty else { throw CancellationError() }
                try await client.deleteAccount(accessToken: token)
            } catch {
                // The account stays for now: so does this iPhone. The extra device goes (best effort).
                try? await client.revokeDevice(id: tokens.device, accessToken: tokens.accessToken)
                return false
            }
            try? keys.delete(account: current.account)
            do {
                try await session.start(tokens)
            } catch {
                return false
            }
            info = nil
            return true
        }
        return false
    }

    /// The session ended on its own (refresh token refused, device revoked): back in with the stored key, or
    /// `.needsSignIn`. Never a new account.
    private func sessionEnded() {
        guard session != nil, state == .ready else { return }
        agenda?.suspend()
        info = nil
        hasLocalKey = false
        watchCode = nil
        state = .starting
        if starting != nil || restoring {
            // The run in flight (or the restore, when it ends) starts again.
            restartRequested = true
        } else {
            launchStart()
        }
    }

    // MARK: - Account

    /// `GET /v1/account` into `info`, and whether this iPhone holds the account's key.
    func refreshAccount() async {
        _ = await readAccount()
    }

    /// `refreshAccount`; `true` when the Cloud answered (a failure keeps the last known `info`).
    private func readAccount() async -> Bool {
        guard let session, state == .ready else { return false }
        var read = false
        do {
            info = try await session.cloudClient.account(accessToken: try await session.accessToken())
            read = true
        } catch {
            if await endIfRefused(error, unauthorized: true) { return false }
        }
        hasLocalKey = await localKey() != nil
        return read
    }

    /// `POST /v1/entitlements/redeem`. An account that has a plan already (`409 already_entitled`) counts as
    /// done: its current plan.
    func redeemInvite(_ code: String) async throws -> Plan {
        let session = try configuredSession()
        do {
            let plan = try await session.cloudClient.redeemInvite(code, accessToken: try await session.accessToken())
            info?.plan = plan
            await refreshAccount()
            return info?.plan ?? plan
        } catch APIError.conflict(let reason, _) where reason == "already_entitled" {
            await refreshAccount()
            guard let plan = info?.plan else { throw APIError.malformedResponse }
            return plan
        } catch {
            _ = await endIfRefused(error)
            throw error
        }
    }

    /// C5: "Sign in with email" (the Zitadel login of 0.3.x, PKCE in the web sheet) brings that account here.
    /// The account here now goes when it is empty (in the Cloud and its key); with something in it the
    /// screen asks first (`RestoreError.currentAccountHasData`) and calls again with `leavingCurrent`, which
    /// leaves it as it is. Any failure before the switch keeps the current account.
    func restoreWithEmail(leavingCurrent: Bool = false) async throws {
        guard let session, let legacy else { throw AccountError.notConfigured }
        guard !restoring else { return }
        // One at a time with `start()`: a start in flight finishes first, and none begins until the restore is
        // done (the web sheet brings the app back to the foreground, which starts the account).
        restoring = true
        defer {
            restoring = false
            if restartRequested {
                restartRequested = false
                launchStart()
            }
        }
        await starting?.value
        // The confirmation's second call reuses the login of the first one: no second page.
        if !(await legacy.isSignedIn) {
            let config = try await legacy.config()
            let (client, provider) = try await legacy.oidc()
            let request = client.authorizationRequest(provider, redirectURI: Self.callback, scopes: config.scopes)
            let callback = try await web.authenticate(url: request.url, callbackScheme: Self.callbackScheme)
            try await legacy.signIn(try await client.exchange(callback: callback, for: request, provider))
        }
        let zitadel = try await legacy.freshAccessToken()
        let client = session.cloudClient
        // The new session stays in memory until the switch; the account gets its key as in the migration.
        let tokens = try await bootstrap(session).migrate(zitadelAccessToken: zitadel)
        let dropNew = { _ = try? await client.revokeDevice(id: tokens.device, accessToken: tokens.accessToken) }
        if await session.isSignedIn, let current = await session.accountID {
            guard current != tokens.account else {
                // Already in that account: the extra device goes, nothing else changes.
                await dropNew()
                await legacy.signOut()
                await refreshAccount()
                return
            }
            do {
                let here = try await client.account(accessToken: try await session.accessToken())
                if here.isEmpty {
                    try await client.deleteAccount(accessToken: try await session.accessToken())
                    try? keys.delete(account: current)
                } else if !leavingCurrent {
                    await dropNew()
                    throw RestoreError.currentAccountHasData(servers: here.servers)
                }
            } catch let error as RestoreError {
                throw error
            } catch {
                await dropNew()
                throw error
            }
        }
        agenda?.suspend()
        try await session.start(tokens)
        await legacy.signOut()
        info = nil
        hasLocalKey = false
        await becameReady(session)
    }

    /// C15: the same key as current and new (`rotate`): every other device of the account is signed out.
    /// Needs this account's key on this iPhone (`hasLocalKey`; `AccountError.signedOut` without it).
    func signOutEverywhere() async throws -> Int {
        let session = try configuredSession()
        guard let stored = await localKey() else { throw AccountError.signedOut }
        do {
            return try await session.cloudClient.signOutEverywhere(key: stored.key, accessToken: try await session.accessToken())
        } catch {
            _ = await endIfRefused(error)
            throw error
        }
    }

    /// Unlinks the linked servers (best effort, with their personal tokens), then `DELETE /v1/account` (the
    /// Cloud deletes the agenda, the devices and the sign-ins). Then nothing of it stays here: its key (also on
    /// the user's other devices) and the pending key, session, the last account, dead keys, onboarding, push keys (`accountDeleted`)
    /// and the local servers. On failure the account stays and `error` says so. Nothing new is made until the
    /// next `start()`.
    func deleteAccount() async {
        guard let session, state == .ready, !restoring else { return }
        error = nil
        let account = await session.accountID
        // First: a sync still running, or a change made meanwhile, must not add back what the deletion removes.
        agenda?.suspend()
        for server in appState.servers where server.linked {
            try? await appState.api(for: server).unlinkAccount()
        }
        do {
            try await session.cloudClient.deleteAccount(accessToken: try await session.accessToken())
        } catch {
            self.error = "Can't delete the account now. " + Self.message(for: error)
            if await endIfRefused(error) { return }
            agenda?.resume()
            return
        }
        // Only this account's key (and the pending one): other accounts' keys in iCloud Keychain are not its to
        // delete (one left behind by a restore, say).
        if let account { try? keys.delete(account: account) }
        try? keys.clearPending()
        try? await session.signOut()
        defaults.removeObject(forKey: Self.lastIDKey)
        DeadKeys.forgetAll(defaults: defaults)
        OnboardingStore.reset(in: defaults)
        if let legacy, await legacy.isSignedIn { await legacy.signOut() }
        info = nil
        hasLocalKey = false
        didMigrate = false
        watchCode = nil
        state = .starting
        await accountDeleted?()
        for server in appState.servers {
            await appState.remove(server.id)
        }
    }

    /// C8: a single use code for the watch to sign in to this account, reused while it has more than a minute
    /// left. `nil` when there is no account yet, the device limit is reached (`403 limit`) or the Cloud fails.
    func enrollCodeForWatch() async -> EnrollCode? {
        guard let session, state == .ready else { return nil }
        if let watchCode, watchCode.expiresAt.timeIntervalSinceNow > 60 { return watchCode }
        do {
            let code = try await session.cloudClient.enrollCode(accessToken: try await session.accessToken())
            watchCode = code
            return code
        } catch {
            _ = await endIfRefused(error)
            return nil
        }
    }

    /// The watch's enroll code goes to the watch as its application context: when the account is ready, back
    /// in the foreground (both end in `becameReady`) and when the watch app gets installed. Only with the
    /// watch app installed, and never an error for the user: a limit or a failure just sends nothing.
    func publishWatchCode() async {
        guard let watchLink, watchLink.isWatchAppInstalled else { return }
        guard let code = await enrollCodeForWatch() else { return }
        watchLink.publish(PhoneLinkContext(enrollCode: code.code, expiresAt: code.expiresAt.timeIntervalSince1970))
    }

    func publishWatchCodeInBackground() {
        guard watchLink?.isWatchAppInstalled == true else { return }
        watchPublishing = Task { [weak self] in await self?.publishWatchCode() }
    }

    // MARK: - Account devices

    /// `GET /v1/devices`: every device signed in to the account (this one is `current`).
    func devices() async throws -> [AccountDevice] {
        try await withReadySession { session in
            try await session.cloudClient.devices(accessToken: try await session.accessToken())
        }
    }

    /// `DELETE /v1/devices/{id}`: signs that device out. This iPhone's own id is never passed (the screen
    /// offers no revoke for the `current` one).
    func revokeDevice(id: String) async throws {
        try await withReadySession { session in
            try await session.cloudClient.revokeDevice(id: id, accessToken: try await session.accessToken())
        }
    }

    // MARK: - Watch sign-in (R11)

    /// Opens `{issuer}/device?user_code=…` (issuer from the build's Cloud, never from the watch) in the same
    /// non-ephemeral web session as the login, so the provider's cookie approves without a new sign-in. The
    /// page never redirects back: closing it (or `watchSignInFinished()`) ends the step with "Check your watch.".
    /// Only for a migrated account (C9): a silent account has no Zitadel user to approve with.
    func approveWatchSignIn(userCode: String, from origin: WatchApprovalOrigin = .settings) async {
        guard let legacy, state == .ready, isMigratedAccount, approvalTask == nil else { return }
        guard DeviceVerification.normalized(userCode) != nil else {
            watchApproval = .failed(Self.badUserCode)
            return
        }
        let issuer: URL
        do {
            issuer = try await legacy.config().issuer
        } catch {
            watchApproval = .failed(Self.message(for: error))
            return
        }
        guard let url = DeviceVerification.url(issuer: issuer, userCode: userCode) else {
            watchApproval = .failed(Self.message(for: AccountError.notConfigured))
            return
        }
        let code = DeviceVerification.normalized(userCode)
        approvingCode = code
        watchApprovalOrigin = origin
        if origin == .settings { settingsApprovedCode = code }
        watchApproval = .open
        defer {
            approvingCode = nil
            watchApprovalOrigin = nil
        }
        let web = web
        let task = Task { () -> (any Error)? in
            do {
                _ = try await web.authenticate(url: url, callbackScheme: Self.callbackScheme)
                return nil
            } catch {
                return error
            }
        }
        approvalTask = task
        let error = await task.value
        approvalTask = nil
        switch error {
        case nil, is CancellationError:
            watchApproval = .done
        default:
            watchApproval = .failed("Can't open the sign-in page.")
        }
    }

    /// M13: the watch said its login is done: the device page (which never comes back on its own) closes.
    func watchSignInFinished() {
        approvalTask?.cancel()
    }

    /// A new approval screen starts clean (unless a page is up).
    func resetWatchApproval() {
        guard approvalTask == nil else { return }
        watchApproval = .idle
    }

    // MARK: - Link

    /// Adds a server with a pairing code made on its host (`wristcall pair --user <you>`), without a personal
    /// token: health → the server's account must be this Cloud → per-server token → `POST /v1/account/link`
    /// with the code → the server's new personal token is verified and saved, linked.
    @discardableResult
    func addLinkedServer(urlText: String, code: String, name: String?) async throws -> ManagedServer {
        guard let url = ServerAddress.parse(urlText) else { throw AddServerError.invalidURL }
        guard let code = PairingCode(code) else { throw LinkError.badCode }
        let session = try configuredSession()
        let api = appState.api(for: url)
        let health = try await api.health()
        let link = try await signingOutIfNeeded {
            let serverToken = try await session.serverToken(for: url, health: health)
            return try await api.linkAccount(serverToken: serverToken, code: code)
        }
        return try await storeLinked(url, link: link, name: name)
    }

    /// Adds the server of a link URL (`WristcallLink.server`, a QR code or `wristcall link` on the server) with
    /// its single-use code: health (the server's account must be this Cloud) → per-server token →
    /// `POST /v1/account/link` (no `Authorization`) → the server's personal token saved, linked (a server
    /// already in the list, same canonical URL, gets the new token) → the agenda's mirror → the watch.
    @discardableResult
    func addLinkedServer(_ server: URL, code: LinkCode) async throws -> ManagedServer {
        guard state == .ready else { throw LinkError.accountNotReady }
        let session = try configuredSession()
        let api = appState.api(for: server)
        let health = try await api.health()
        // Refused before any Cloud call (`serverToken` checks again).
        guard health.account != nil else { throw AccountError.serverWithoutAccount }
        let link = try await signingOutIfNeeded {
            let serverToken = try await session.serverToken(for: server, health: health)
            return try await api.linkAccount(serverToken: serverToken, code: code)
        }
        return try await storeLinked(server, link: link, name: nil)
    }

    /// The steps after a link by code: the personal token saved (the agenda mirrors it through the app's
    /// hooks), then the watch gets a pairing code for it.
    private func storeLinked(_ url: URL, link: AccountLink, name: String?) async throws -> ManagedServer {
        guard link.linked, let token = link.apiToken else { throw APIError.malformedResponse }
        let saved: ManagedServer
        do {
            saved = try await appState.addServer(urlText: url.absoluteString, token: token, name: name, linked: true)
        } catch {
            // The code is spent and the token exists on the server: say how to clean it up (never the token).
            throw LinkError.notSaved
        }
        pairWatch(with: saved)
        return saved
    }

    /// Sends the watch a pairing code for `server` without holding the screen: `WatchLink` asks the server
    /// for a code only when a watch with the app is there.
    private func pairWatch(with server: ManagedServer) {
        guard let watchLink else { return }
        let api = appState.api(for: server)
        watchPairing = Task { await watchLink.send(server: server, api: api) }
    }

    /// Links a saved server (personal token) to the account. The server's `409` means another user there has
    /// this account: an error, never counted as linked.
    func link(_ server: ManagedServer) async throws {
        let session = try configuredSession()
        let api = appState.api(for: server)
        let health = try await api.health()
        let link = try await signingOutIfNeeded {
            let serverToken = try await session.serverToken(for: server.url, health: health)
            return try await api.linkAccount(serverToken: serverToken)
        }
        guard link.linked else { throw APIError.malformedResponse }
        try await appState.markLinked(server.id)
    }

    // MARK: - Web sign-in

    /// `POST /v1/web-sessions/{id}/inspect`: what the browser behind a web sign-in code looks like. `key` is
    /// the approve key from its QR code.
    func inspectWebSession(id: String, key: String) async throws -> WebSessionInfo {
        try await withReadySession { session in
            try await session.cloudClient.inspectWebSession(id: id, key: key, accessToken: try await session.accessToken())
        }
    }

    /// `POST /v1/web-sessions/{id}/approve`: the browser signs in to this account.
    func approveWebSession(id: String, key: String) async throws {
        try await withReadySession { session in
            try await session.cloudClient.approveWebSession(id: id, key: key, accessToken: try await session.accessToken())
        }
    }

    private func withReadySession<T>(_ work: (DeviceSession) async throws -> T) async throws -> T {
        guard state == .ready else { throw LinkError.accountNotReady }
        let session = try configuredSession()
        do {
            return try await work(session)
        } catch {
            _ = await endIfRefused(error)
            throw error
        }
    }

    // MARK: - Messages

    /// Text for a failed link URL (`addLinkedServer(_:code:)`). Copy may hold Markdown code spans.
    static func linkMessage(for error: any Error) -> String {
        switch error {
        case AccountError.serverWithoutAccount, APIError.notConfigured:
            "This server isn't set up for wristcall accounts."
        case AccountError.foreignIssuer:
            "This server uses another wristcall account service."
        case APIError.invalidCode:
            "This code was already used or has expired. Run `wristcall link` on the server for a new one."
        case APIError.conflict:
            "Your account is already linked to another user on this server."
        case APIError.invalidAccountToken:
            "The server didn't accept the sign-in. Try again."
        case APIError.rateLimited, APIError.retryLater:
            "Too many attempts. Try again in a minute."
        case LinkError.accountNotReady:
            settingUpMessage
        // The server's health (`ServerPushClient`) fails with `PairingError`.
        case PairingError.network:
            "Can't reach the server."
        case PairingError.rateLimited:
            "Too many attempts. Try again in a minute."
        case is PairingError:
            "This doesn't look like a wristcall server."
        default:
            fixedMessage(for: error)
        }
    }

    /// Text for a failed web sign-in approval.
    static func webMessage(for error: any Error) -> String {
        switch error {
        case APIError.notFound:
            "This code expired. Reload the page for a new one."
        case APIError.limit:
            "Too many web sessions are open. Sign out of one in Devices first."
        case APIError.rateLimited, APIError.retryLater:
            "Too many attempts. Try again in a minute."
        case LinkError.accountNotReady:
            settingUpMessage
        default:
            fixedMessage(for: error)
        }
    }

    /// `message(for:)` without the replies that carry the server's own words (`forbidden`, `limit`,
    /// `notLinked`, `invalid`, `unavailable`): link screens show only the app's copy.
    private static func fixedMessage(for error: any Error) -> String {
        switch error {
        case APIError.unavailable:
            "The server is unavailable. Try again later."
        case APIError.forbidden, APIError.limit, APIError.notLinked, APIError.invalid:
            "The server refused the request."
        default:
            message(for: error)
        }
    }

    static let settingUpMessage = "Setting up your account…"

    /// Short text for the screen. Never the error's own text when it may carry what was sent.
    static func message(for error: any Error) -> String {
        switch error {
        case AccountError.foreignIssuer:
            return "This server uses another account service."
        case AccountError.serverWithoutAccount:
            return "This server has no account set up."
        case AccountError.signedOut:
            return "Sign in again."
        case AccountError.notConfigured:
            return "wristcall Cloud is not set up for this app."
        case LinkError.badCode:
            return "Enter the 8-digit code."
        case LinkError.notSaved:
            return notSavedMessage
        case LinkError.accountNotReady:
            return settingUpMessage
        case let failure as AddServerError:
            return failure.message
        case APIError.conflict:
            return "Already linked to another user on this server."
        case APIError.notConfigured:
            return "This server has no account set up."
        case let failure as APIError:
            return failure.message
        case is OIDCError:
            return "The sign-in did not complete. Try again."
        case is CancellationError:
            return "Cancelled."
        default:
            return "Something went wrong. Try again."
        }
    }

    static let notSavedMessage = "Your account is linked, but this iPhone could not save the server. The server "
        + "made a personal token for it that stays valid: on the server's host, find it with "
        + "\"wristcall users tokens list --user <you>\" (named \"account link\") and revoke it with "
        + "\"wristcall users tokens revoke <id>\". Then add the server again."

    /// Short text for a failed email restore.
    static func signInMessage(for error: any Error) -> String {
        switch error {
        case OIDCError.authorizationFailed:
            "The sign-in was refused."
        case let failure as APIError:
            if case .network = failure { "Can't reach wristcall Cloud." } else { "wristcall Cloud refused the sign-in." }
        case AccountError.notConfigured:
            message(for: error)
        case is WebAuthenticatorError:
            "Can't open the sign-in page."
        default:
            "The sign-in did not complete. Try again."
        }
    }

    /// How long to wait before the next try of a start that failed this way; `nil`: no retry.
    nonisolated static func retryDelay(for error: any Error) -> TimeInterval? {
        switch error {
        case APIError.retryLater(let seconds):
            // Clamped: a broken `Retry-After` neither spins nor parks the start for days.
            return seconds.isFinite ? min(max(seconds, 1), 3600) : 3600
        case APIError.rateLimited:
            return 60
        case APIError.network, APIError.unavailable, OIDCError.network:
            return 30
        case APIError.unexpectedStatus(let status) where status >= 500:
            return 30
        default:
            return nil
        }
    }

    // MARK: - Plumbing

    private func configuredSession() throws -> DeviceSession {
        guard let session else { throw AccountError.notConfigured }
        return session
    }

    private func bootstrap(_ session: DeviceSession) -> AccountBootstrap {
        let sleep = sleep
        return AccountBootstrap(
            client: session.cloudClient, keys: keys, device: device, defaults: defaults,
            waitForKeys: { await sleep(2) }
        )
    }

    /// The stored key of the current account, unless the Cloud refused it before.
    private func localKey() async -> StoredAccountKey? {
        guard let account = await session?.accountID else { return nil }
        return ((try? keys.keys()) ?? []).first { $0.account == account && !DeadKeys.isDead($0, defaults: defaults) }
    }

    /// A refused session: the refresh token (`.signedOut`, the session is gone already) or the device itself
    /// (`revoked`, and with `unauthorized` a plain `401` of a read). Forgets the session and gets back in.
    private func endIfRefused(_ error: any Error, unauthorized: Bool = false) async -> Bool {
        switch error {
        case AccountError.signedOut:
            break
        case APIError.revoked:
            try? await session?.signOut()
        case APIError.unauthorized where unauthorized:
            try? await session?.signOut()
        default:
            return false
        }
        sessionEnded()
        return true
    }

    /// Runs `work`; a refused refresh token (`.signedOut`) also gets the account back in.
    private func signingOutIfNeeded<T>(_ work: () async throws -> T) async throws -> T {
        do {
            return try await work()
        } catch AccountError.signedOut {
            sessionEnded()
            throw AccountError.signedOut
        }
    }

    /// Hangs the agenda on the app's hooks, keeping the ones already there. The agenda part only queues a
    /// background job: managing servers and agents never waits for the Cloud.
    private func installHooks() {
        guard let agenda else { return }
        var hooks = appState.hooks
        let added = hooks.serverAdded
        let changed = hooks.serverChanged
        let removed = hooks.serverRemoved
        let agents = hooks.agentsChanged
        hooks.serverAdded = { [weak agenda] server in
            await added?(server)
            agenda?.serverAdded(server)
        }
        hooks.serverChanged = { [weak agenda] server in
            await changed?(server)
            agenda?.serverChanged(server)
        }
        hooks.serverRemoved = { [weak agenda] server in
            await removed?(server)
            agenda?.serverRemoved(server)
        }
        hooks.agentsChanged = { [weak agenda] server, list in
            await agents?(server, list)
            agenda?.agentsChanged(server, list)
        }
        appState.hooks = hooks
    }
}
