import Foundation
import Observation
import WristcallKit

/// What `SetupModel` needs from the Cloud: the watch's device session and the account's agenda (live: Kit's
/// `WatchEnrollment`, `DeviceSession` and `AccountPairing`), fake in tests.
protocol SetupBackend: Sendable {
    /// The account of the saved device session, `nil` without one.
    func restore() async -> String?
    /// `WatchEnrollment.enroll(code:tryLegacy:)` + `DeviceSession.start`; returns the account in `.enrolled`.
    func enroll(code: PhoneLinkContext?, tryLegacy: Bool) async throws -> SetupEnrollResult
    /// The account's servers (`GET /v1/servers`). Throws `AccountError.signedOut` when the session is gone.
    func agenda() async throws -> [CloudServer]
    /// `AccountPairing.pair(_:deviceName:allowApproval: false)`: a server that asks its owner is skipped.
    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome
    /// Deletes this watch's device in the Cloud (best effort) and the saved session.
    func signOut() async
}

/// `WatchEnrollment.Result` as the app sees it: the account instead of the tokens.
enum SetupEnrollResult: Equatable {
    case enrolled(account: String), nothing, deviceLimit
}

/// The watch's first open and its automatic agenda sync (decisions C2 to C5, C7): the saved session, else a loop
/// that tries the account key, the iPhone's enroll code and a 0.4.0 sign-in until one works, then pairs the
/// agenda's servers that need no approval. With servers already paired everything happens quietly (no setup
/// screen); without, `phase` drives "Setting up" and, after `timeout`, "Open wristcall on your iPhone".
@MainActor
@Observable
final class SetupModel {
    enum Phase: Equatable { case idle, signingIn, gettingAgents, waitingForPhone, noAgents, ready }

    static let deviceLimitMessage = "Device limit reached. Remove a device in wristcall on your iPhone."
    static let otherAccountMessage = "This watch uses a different wristcall account than your iPhone."
    /// `UserDefaults` key: "Sign out of this watch" pauses the automatic setup until "Set up again" (C4).
    static let pausedDefaultsKey = "setup.paused"

    private(set) var phase: Phase = .idle
    /// The Cloud account of this watch's device session.
    private(set) var account: String?
    private(set) var message: String?
    private(set) var isPaused: Bool
    /// The account of the last `PhoneLinkContext` (`nil` from an iPhone 0.2.0).
    private(set) var phoneAccount: String?
    var isSignedIn: Bool { account != nil }
    /// The setup screen replaces the pairing screen: no server, and not adding one.
    var showsSetup: Bool { model.phase == .unpaired && !model.isAddingServer }
    /// Told the account once it is known (a restored session at once, a new one after its first sync) and `nil`
    /// when it is gone, for the watch's link context (C7).
    var onAccountReady: (@MainActor (String?) -> Void)?
    /// Device flow or manual sync running (`AccountLoginModel.isRunning`): the automatic enrollment and sync do
    /// not start.
    var isManualRunning: @MainActor () -> Bool = { false }
    /// An enrollment round or a sync is in flight (`AccountLoginModel.isSetupBusy`): the manual login does not
    /// start. The wait between two rounds is not busy: it is when "Use a code instead" is needed.
    var isBusy: Bool { enrolling > 0 || syncTask.map { !$0.isCancelled } ?? false }

    @ObservationIgnored private let backend: any SetupBackend
    @ObservationIgnored private let model: AppModel
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private let timeout: TimeInterval
    @ObservationIgnored private let keyPoll: Duration
    @ObservationIgnored private let syncInterval: TimeInterval
    @ObservationIgnored private let now: () -> Date
    @ObservationIgnored private let sleep: @Sendable (Duration) async throws -> Void

    /// `restore()` has answered: until then a code is only kept.
    @ObservationIgnored private var hasRestored = false
    /// The newest code from the iPhone not yet refused.
    @ObservationIgnored private var pendingCode: PhoneLinkContext?
    /// Codes a round sent that got `.nothing` (expired, used or refused): never sent again.
    @ObservationIgnored private var refusedCodes: Set<String> = []
    @ObservationIgnored private var loopTask: Task<Void, Never>?
    @ObservationIgnored private var loopID = 0
    /// When the running loop started, for `timeout`.
    @ObservationIgnored private var loopStart = Date.distantPast
    /// The wait between two rounds; cancelled to wake the loop.
    @ObservationIgnored private var waitTask: Task<Void, any Error>?
    /// A code or an activation arrived during a round: the loop goes again at once.
    @ObservationIgnored private var wakeRequested = false
    /// An activation asked for the 0.4.0 sign-in on the next round.
    @ObservationIgnored private var legacyRequested = false
    /// `backend.enroll` calls running (a counter: a cancelled loop's call may still end after a new loop's starts).
    @ObservationIgnored private var enrolling = 0
    @ObservationIgnored private var syncTask: Task<Void, Never>?
    @ObservationIgnored private var syncID = 0
    /// A new enrollment waits for its first sync to be announced.
    @ObservationIgnored private var announcePending = false
    @ObservationIgnored private var lastSync: Date?

    init(backend: any SetupBackend, model: AppModel, defaults: UserDefaults = .standard,
         timeout: Duration = .seconds(10), keyPoll: Duration = .seconds(5), syncInterval: TimeInterval = 60,
         now: @escaping () -> Date = Date.init,
         sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.backend = backend
        self.model = model
        self.defaults = defaults
        self.timeout = Double(timeout.components.seconds) + Double(timeout.components.attoseconds) / 1e18
        self.keyPoll = keyPoll
        self.syncInterval = syncInterval
        self.now = now
        self.sleep = sleep
        isPaused = defaults.bool(forKey: Self.pausedDefaultsKey)
    }

    // MARK: - Actions

    /// At launch: the saved session (announced at once, then a sync), else the loop (returns without waiting
    /// for it). Paused, nothing starts.
    func start() async {
        let restored = await backend.restore()
        hasRestored = true
        if let restored {
            loopTask?.cancel()
            loopTask = nil
            setAccount(restored)
            onAccountReady?(restored)
            phase = .ready
            sync()
        } else if account == nil, !isPaused {
            startLoop(tryLegacy: true)
        }
    }

    /// The iPhone's application context (it can arrive twice: activation and `didReceiveApplicationContext`).
    /// Signed in, only the account is compared (C3); otherwise a new code wakes the loop.
    func receive(_ context: PhoneLinkContext) {
        phoneAccount = context.account
        if account != nil { checkPhoneAccount() }
        guard !refusedCodes.contains(context.enrollCode),
              pendingCode?.enrollCode != context.enrollCode else { return }
        // Kept even when signed in: after "Sign out" + "Set up again" it enrolls into the iPhone's account (C3).
        pendingCode = context
        guard account == nil, hasRestored, !isPaused else { return }
        startLoop(tryLegacy: false)
    }

    /// The app came to the foreground: without an account a round (with the 0.4.0 sign-in), with one a sync at
    /// most every `syncInterval`.
    func becameActive() async {
        guard hasRestored, !isPaused else { return }
        if account == nil {
            startLoop(tryLegacy: true)
        } else if lastSync.map({ now().timeIntervalSince($0) >= syncInterval }) ?? true {
            sync()
        }
    }

    /// The device flow ("Use a code instead") finished: no sync, the login pairs the servers itself.
    func didSignIn(account: String) {
        stopLoop()
        // A loop enrollment whose sync is still running must not pair or announce after this.
        syncTask?.cancel()
        announcePending = false
        isPaused = false
        defaults.removeObject(forKey: Self.pausedDefaultsKey)
        message = nil
        setAccount(account)
        lastSync = now()
        onAccountReady?(account)
        phase = model.servers.isEmpty ? .noAgents : .ready
    }

    /// "Sign out of this watch" (C4): the device session goes, servers stay, and the automatic setup pauses until
    /// "Set up again".
    func signOut() async {
        // Paused before the first await: an activation or a code meanwhile starts nothing.
        isPaused = true
        defaults.set(true, forKey: Self.pausedDefaultsKey)
        let loop = loopTask
        let sync = syncTask
        stopLoop()
        syncTask?.cancel()
        syncTask = nil
        announcePending = false
        account = nil
        message = nil
        phase = .idle
        onAccountReady?(nil)
        await loop?.value
        await sync?.value
        await backend.signOut()
    }

    /// "Set up again": the automatic setup runs again (no key is touched).
    func setUpAgain() async {
        defaults.removeObject(forKey: Self.pausedDefaultsKey)
        isPaused = false
        await start()
    }

    /// Waits for the loop and the sync running now (and any one they start). A loop waiting for the next key
    /// poll does not end: callers (tests) use this only when it will.
    func settle() async {
        while let task = loopTask ?? syncTask {
            await task.value
        }
    }

    // MARK: - Enrollment loop

    /// One loop at a time: a running one is woken instead (and asked for the 0.4.0 sign-in if `tryLegacy`).
    private func startLoop(tryLegacy: Bool) {
        guard !isPaused else { return }
        if loopTask != nil {
            legacyRequested = legacyRequested || tryLegacy
            wakeRequested = true
            waitTask?.cancel()
            return
        }
        loopID += 1
        let id = loopID
        loopStart = now()
        wakeRequested = false
        legacyRequested = false
        if message == Self.deviceLimitMessage { message = nil }
        if model.servers.isEmpty { phase = .signingIn }
        loopTask = Task { [self] in
            await runLoop(tryLegacy: tryLegacy)
            if loopID == id { loopTask = nil }
        }
    }

    private func stopLoop() {
        loopTask?.cancel()
        loopTask = nil
        waitTask?.cancel()
    }

    private func runLoop(tryLegacy first: Bool) async {
        var tryLegacy = first
        while !Task.isCancelled {
            updateWaitingPhase()
            guard !isManualRunning() else {
                // The device flow makes the session itself (a second one would leave an extra device in the
                // Cloud); the round, its 0.4.0 sign-in included, waits.
                guard model.servers.isEmpty else { return }
                await waitForNextRound()
                continue
            }
            let code = pendingCode
            let legacy = tryLegacy || legacyRequested
            tryLegacy = false
            legacyRequested = false
            wakeRequested = false

            let result: SetupEnrollResult?
            enrolling += 1
            do {
                result = try await backend.enroll(code: code, tryLegacy: legacy)
            } catch {
                // Network, `429` or anything else: try again later.
                result = nil
            }
            enrolling -= 1
            guard !Task.isCancelled else { return }

            switch result {
            case .enrolled(let enrolled)?:
                pendingCode = nil
                setAccount(enrolled)
                if model.servers.isEmpty { phase = .gettingAgents }
                await sync(announceAfter: true)?.value
                // The new session was refused by the agenda at once: enroll again, after the usual wait (never
                // in a tight loop).
                guard account == nil, !Task.isCancelled else { return }
                wakeRequested = false
                legacyRequested = false
            case .nothing?:
                if let code {
                    refusedCodes.insert(code.enrollCode)
                    if pendingCode?.enrollCode == code.enrollCode { pendingCode = nil }
                }
            case .deviceLimit?:
                message = Self.deviceLimitMessage
                return
            case nil:
                break
            }

            updateWaitingPhase()
            if wakeRequested { continue }
            // With servers the setup is quiet: one round per launch or activation, never every `keyPoll`.
            guard model.servers.isEmpty else { return }
            await waitForNextRound()
        }
    }

    /// Without servers and past `timeout` since the loop started: "Open wristcall on your iPhone" (the loop goes on).
    private func updateWaitingPhase() {
        guard model.servers.isEmpty else { return }
        if now().timeIntervalSince(loopStart) >= timeout {
            phase = .waitingForPhone
        } else if phase != .waitingForPhone {
            phase = .signingIn
        }
    }

    /// `keyPoll`, or less when a code or an activation wakes the loop (it cancels `waitTask`).
    private func waitForNextRound() async {
        let sleep = sleep
        let duration = keyPoll
        let task = Task { try await sleep(duration) }
        waitTask = task
        await withTaskCancellationHandler {
            _ = await task.result
        } onCancel: {
            task.cancel()
        }
        if waitTask == task { waitTask = nil }
    }

    // MARK: - Sync

    /// One sync at a time; a second request joins the running one. With `announceAfter` (a new enrollment) the
    /// account is announced when it ends.
    @discardableResult
    private func sync(announceAfter: Bool = false) -> Task<Void, Never>? {
        if announceAfter { announcePending = true }
        // A cancelled sync (device flow finished) stays in `syncTask` until it ends, but is not joined.
        if let syncTask, !syncTask.isCancelled { return syncTask }
        guard !isManualRunning() else {
            // The device flow or the manual sync pairs instead; the next activation tries again.
            finishSync()
            return nil
        }
        lastSync = now()
        syncID += 1
        let id = syncID
        let task = Task { [self] in
            await runSync(id: id)
            if syncID == id { syncTask = nil }
        }
        syncTask = task
        return task
    }

    private func runSync(id: Int) async {
        do {
            let agenda = try await backend.agenda()
            let candidates = AccountPairing.candidates(agenda, paired: model.servers.map(\.credentials.serverURL))
            for server in candidates {
                guard !Task.isCancelled else { return }
                // A call or a pairing on screen: nothing is asked now; the next activation goes on.
                guard model.canPairWithAccount else { break }
                let outcome = await backend.pair(server, deviceName: model.deviceName)
                guard !Task.isCancelled else { return }
                // `.pending` never comes (no approval asked); `.skipped` waits for the iPhone (C5, C6).
                if case .paired(_, let device) = outcome {
                    try? await model.addPaired(device, server: server)
                }
            }
        } catch AccountError.signedOut {
            guard !Task.isCancelled else { return }
            // The session was revoked: enroll again (quietly when there are servers).
            account = nil
            announcePending = false
            if syncID == id { syncTask = nil }
            onAccountReady?(nil)
            startLoop(tryLegacy: true)
            return
        } catch {
            // Network or Cloud error: what is paired stays; the next activation tries again.
        }
        guard !Task.isCancelled else { return }
        finishSync()
    }

    private func finishSync() {
        if announcePending {
            announcePending = false
            onAccountReady?(account)
        }
        phase = model.servers.isEmpty ? .noAgents : .ready
    }

    // MARK: - Account

    private func setAccount(_ value: String) {
        account = value
        checkPhoneAccount()
    }

    /// Signed in, an iPhone announcing another account shows `otherAccountMessage` (C3).
    private func checkPhoneAccount() {
        if let account, let phoneAccount, phoneAccount != account {
            message = Self.otherAccountMessage
        } else if message == Self.otherAccountMessage {
            message = nil
        }
    }
}
