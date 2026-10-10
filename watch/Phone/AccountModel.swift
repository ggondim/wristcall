import Foundation
import Observation
import WristcallKit

enum LinkError: Error, Equatable {
    /// The code is not 8 digits (nothing was sent).
    case badCode
    /// The server linked the account and issued a personal token, but the app could not verify or save it.
    case notSaved
}

/// The central account on this iPhone (R5, R6): sign in (Authorization Code with PKCE in the system's web
/// sheet), sign out, delete the agenda, link servers to the account, and keep the agenda in sync.
///
/// Optional: without a Cloud URL in the build (`WristcallCloudURL`) the state is `.unavailable` and the
/// account screens are hidden. Secrets (tokens, codes, the end-session URL) never reach a log or a message.
@MainActor
@Observable
final class AccountModel {
    enum State: Equatable {
        case unavailable, signedOut, signingIn, signedIn
        case failed(String)
    }

    static let callback = URL(string: "wristcall://auth/callback")!
    static let logoutCallback = URL(string: "wristcall://auth/logout")!
    static let callbackScheme = "wristcall"

    private(set) var state: State
    /// The last failure of an action taken while signed in (deleting the account), for the screen.
    private(set) var error: String?
    let cloudURL: URL?
    let appState: AppState
    /// The agenda mirror while an account is possible (`nil` when `.unavailable`).
    let agenda: AgendaSync?
    /// Runs after the Cloud deleted the account, before signing out: the app drops its own push keys there.
    @ObservationIgnored var accountDeleted: (@MainActor () async -> Void)?

    @ObservationIgnored private let session: AccountSession?
    @ObservationIgnored private let web: any WebAuthenticator

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

    init(cloudURL: URL?, session: AccountSession?, web: any WebAuthenticator, state appState: AppState) {
        let session = cloudURL == nil ? nil : session
        self.cloudURL = session == nil ? nil : cloudURL
        self.session = session
        self.web = web
        self.appState = appState
        self.state = session == nil ? .unavailable : .signedOut
        self.agenda = session.map { AgendaSync(session: $0, state: appState) }
        agenda?.onSignedOut = { [weak self] in await self?.sessionEnded() }
        installHooks()
    }

    /// The build's Cloud URL (Info.plist `WristcallCloudURL`): `https://`, or `http://` on localhost only.
    /// Empty, unexpanded or anything else: no account.
    nonisolated static func cloudURL(fromInfoValue value: String?) -> URL? {
        guard let value, value.contains("://") else { return nil }
        return ServerAddress.parse(value)
    }

    /// At launch, once the servers are read: picks up a stored session and mirrors the servers.
    func restore() async {
        guard let session else { return }
        guard await session.isSignedIn else {
            if state == .signedIn { state = .signedOut }
            return
        }
        state = .signedIn
        agenda?.pushAll()
    }

    // MARK: - Sign in, sign out, delete

    /// `/v1/config` and discovery → PKCE request → web sheet → code exchange → tokens stored → agenda pushed.
    /// Closing the sheet goes back to `.signedOut` without a message.
    func signIn() async {
        guard let session, state != .signingIn else { return }
        state = .signingIn
        error = nil
        do {
            let config = try await session.config()
            let (client, provider) = try await session.oidc()
            let request = client.authorizationRequest(provider, redirectURI: Self.callback, scopes: config.scopes)
            let callback = try await web.authenticate(url: request.url, callbackScheme: Self.callbackScheme)
            let tokens = try await client.exchange(callback: callback, for: request, provider)
            try await session.signIn(tokens)
            state = .signedIn
        } catch is CancellationError {
            state = .signedOut
            return
        } catch {
            state = .failed(Self.signInMessage(for: error))
            return
        }
        agenda?.pushAll()
    }

    /// Deletes the tokens, revokes the refresh token, then opens the provider's end-session page (so the
    /// next sign-in can pick another user). Servers and their tokens stay.
    func signOut() async {
        guard let session else { return }
        // First: a sync still running must not write the agenda back once the tokens are gone.
        agenda?.suspend()
        let endSession = await session.signOut(postLogoutRedirect: Self.logoutCallback)
        appState.forgetAccount()
        state = .signedOut
        error = nil
        agenda?.resume()
        if let endSession {
            // Closing the page or failing to open it changes nothing: the app is signed out already.
            _ = try? await web.authenticate(url: endSession, callbackScheme: Self.callbackScheme)
        }
    }

    /// `DELETE /v1/account` (the Cloud deletes the agenda and the user at the identity provider; push registrations are
    /// anonymous), then `accountDeleted` (the app drops its own push keys) and sign out.
    /// On failure the session stays and `error` says so.
    func deleteAccount() async {
        guard let session, state == .signedIn else { return }
        error = nil
        // First: a sync still running, or a change made meanwhile, must not add back what the deletion removes.
        agenda?.suspend()
        do {
            try await session.cloudClient.deleteAccount(accessToken: try await session.accessToken())
        } catch AccountError.signedOut {
            agenda?.resume()
            await sessionEnded()
            return
        } catch {
            agenda?.resume()
            self.error = "Can't delete the account now. " + Self.message(for: error)
            return
        }
        await accountDeleted?()
        await signOut()
    }

    // MARK: - Watch sign-in (R11)

    /// Opens `{issuer}/device?user_code=…` (issuer from the build's Cloud, never from the watch) in the same
    /// non-ephemeral web session as the login, so the provider's cookie approves without a new sign-in. The
    /// page never redirects back: closing it (or `watchSignInFinished()`) ends the step with "Check your watch.".
    func approveWatchSignIn(userCode: String, from origin: WatchApprovalOrigin = .settings) async {
        guard let session, state == .signedIn, approvalTask == nil else { return }
        guard DeviceVerification.normalized(userCode) != nil else {
            watchApproval = .failed(Self.badUserCode)
            return
        }
        let issuer: URL
        do {
            issuer = try await session.config().issuer
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
        let session = try signedInSession()
        let api = appState.api(for: url)
        let health = try await api.health()
        let link = try await signingOutIfNeeded {
            let serverToken = try await session.serverToken(for: url, health: health)
            return try await api.linkAccount(serverToken: serverToken, code: code)
        }
        guard link.linked, let token = link.apiToken else { throw APIError.malformedResponse }
        do {
            return try await appState.addServer(urlText: url.absoluteString, token: token, name: name, linked: true)
        } catch {
            // The code is spent and the token exists on the server: say how to clean it up (never the token).
            throw LinkError.notSaved
        }
    }

    /// Links a saved server (personal token) to the account. The server's `409` means another user there has
    /// this account: an error, never counted as linked.
    func link(_ server: ManagedServer) async throws {
        let session = try signedInSession()
        let api = appState.api(for: server)
        let health = try await api.health()
        let link = try await signingOutIfNeeded {
            let serverToken = try await session.serverToken(for: server.url, health: health)
            return try await api.linkAccount(serverToken: serverToken)
        }
        guard link.linked else { throw APIError.malformedResponse }
        try await appState.markLinked(server.id)
    }

    // MARK: - Messages

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

    private static func signInMessage(for error: any Error) -> String {
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

    // MARK: - Plumbing

    private func signedInSession() throws -> AccountSession {
        guard let session else { throw AccountError.notConfigured }
        return session
    }

    /// Runs `work`; a refused refresh token (`.signedOut`) also moves the screen to signed out.
    private func signingOutIfNeeded<T>(_ work: () async throws -> T) async throws -> T {
        do {
            return try await work()
        } catch AccountError.signedOut {
            await sessionEnded()
            throw AccountError.signedOut
        }
    }

    /// The session ended on its own (refresh token refused): the tokens are gone already.
    private func sessionEnded() async {
        guard session != nil, state != .signedOut else { return }
        agenda?.invalidate()
        appState.forgetAccount()
        state = .signedOut
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
