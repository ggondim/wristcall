import Foundation
import Observation
import WristcallKit

/// Which screen the app shows.
enum AppPhase: Equatable {
    /// Reading the Keychain.
    case launching
    /// No server yet, or adding one (`AppModel.isAddingServer`): the pairing screen.
    case unpaired
    /// A pairing request is running. `requestId` (4 digits) is set while waiting for the
    /// owner to run `wristcall devices approve <requestId>` (flow B).
    case pairing(requestId: String?)
    /// The agents of every server. Each server loads on its own (`ServerEntry.status`).
    case home
    /// The Keychain could not be read (locked before the first unlock, or another error): "Retry".
    case unavailable
    /// A call is active with this agent.
    case inCall(AgentTarget)
    /// After a one-way call: what happened to the recording (`AppModel.callResult`).
    case callResult
}

/// What the call screen shows while a call is open (task 10).
enum CallActivity: Equatable {
    /// Waiting for CallKit, the socket and `session.ready`.
    case connecting
    /// The microphone is open and the server is listening.
    case listening
    /// The user's turn closed; the server is transcribing and answering.
    case thinking
    /// The agent's audio is playing.
    case agentSpeaking
    /// One-way call: the server is recording (decision W9).
    case recording
    /// One-way call muted from the system UI: audio sent meanwhile is dropped, the call goes on.
    case paused

    var label: String {
        switch self {
        case .connecting: "Connecting…"
        case .listening: "Listening"
        case .thinking: "Thinking…"
        case .agentSpeaking: "Speaking"
        case .recording: "Recording"
        case .paused: "Paused"
        }
    }
}

/// What the call layer (tasks 8 to 10) needs to open a call.
struct CallRequest: Sendable, Equatable {
    let credentials: Credentials
    /// The agent called; its `displayName` is the CallKit caller name.
    let target: AgentTarget
    /// How the user's turn ends (`session.start` `turn_end`); `nil` leaves it to the agent (decision W5).
    var turnEnd: TurnEnd?
}

/// Implemented by the `CallCoordinator` (task 10). `AppModel` keeps a strong reference;
/// the handler must hold the model weakly.
@MainActor
protocol CallHandling: AnyObject {
    /// Start a CallKit call for `request`. Report its end, whatever the cause, with
    /// `AppModel.callDidEnd(_:callID:)`.
    func startCall(_ request: CallRequest)
    /// The user tapped "End" in the app. Report the end with `AppModel.callDidEnd(_:callID:)`.
    func endCall()
}

/// Push notifications (task 12, `PushCoordinator`): told when servers come and go, so each one
/// has its push key, and when a one-way result first shows. Only the push build sets one.
@MainActor
protocol PushHandling: AnyObject {
    /// A server was paired (or paired again): register its push key.
    func serverAdded(_ credentials: Credentials)
    /// The user removes this server; runs before `DELETE /v1/me`, while its token still works.
    func serverWillBeRemoved(_ credentials: Credentials) async
    /// The server left the list (removed by the user or by a `401`).
    func serverRemoved(_ credentials: Credentials)
    /// The result screen of a one-way call opened after the call.
    func oneWayResultShown()
}

/// State and actions behind every screen: the paired servers and their agents, pairing (flows A,
/// A' and B), removing a server, settings and the entry point of a call.
@Observable
@MainActor
final class AppModel {
    /// Texts shown to the user.
    enum Message {
        static let invalidCode = "Invalid or expired code."
        static let codeNotFound = "Code not found. Check it or use the server URL."
        static let insecureServer = "The directory returned an insecure server address."
        static let directoryUnreachable = "Can't reach the pairing directory."
        static let rateLimited = "Too many attempts. Try again in a minute."
        static let unreachable = "Can't reach the server."
        static let unexpected = "Unexpected reply from the server."
        static let expired = "The request expired. Try again."
        static let revoked = "This watch was removed on the server. Pair again."
        static let locked = "Unlock the watch to continue."
        static let invalidURL = "Use an https:// address."
        static let serverURLNeeded = "Enter the server URL first."
        static let keychain = "Couldn't access the pairing on this watch."
        static let unpairedOffline = "Unpaired here. The server was unreachable: revoke this watch there."
        static let connectionLost = "Connection lost"
        static let callNotStarted = "Couldn't start the call."
        static let microphoneUnavailable = "Microphone unavailable."
        static let noConnection = "No connection"
        static let agentNotFound = "Agent not found."
        static let unsupportedAgent = "Update Wristcall to call this agent."
        static let nothingSent = "Nothing was sent."
        /// Name on the result screen of a call whose agent the server no longer lists.
        static let unknownAgent = "Last call"

        static func removed(_ host: String) -> String { "\(host): this watch was removed on the server." }
        static func cantReach(_ host: String) -> String { "Can't reach \(host)." }
    }

    static let directoryDefaultsKey = "pairingDirectoryURL"
    /// Sent as `device_name`; the owner sees it in `wristcall devices list`.
    static let defaultDeviceName = "Apple Watch"

    private(set) var phase: AppPhase = .launching
    /// The last thing worth telling the user (an error, "Connection lost"); `nil` when there is nothing.
    private(set) var message: String?
    /// Server typed by the user (flows A' and B); `nil` resolves the code with the directory (flow A).
    private(set) var customServerURL: URL?
    /// Where codes are resolved. Persisted in `UserDefaults`.
    private(set) var directoryURL: URL
    /// The paired servers, in the order the grid shows them (the order they were paired in).
    private(set) var servers: [ServerEntry] = []
    /// The pairing screen is up to add a server to the ones already paired.
    private(set) var isAddingServer = false
    /// Servers whose `DELETE /v1/me` is running.
    private(set) var removingServerIDs: Set<String> = []
    /// What the call screen shows; meaningful only in `.inCall`.
    private(set) var callActivity: CallActivity = .connecting
    /// The result screen of the last one-way call; set only in `.callResult`.
    private(set) var callResult: CallResultModel?
    /// The catalog last handed to `onAgentsChanged` (until then, the one the last run saved).
    private(set) var catalog: [CatalogAgent]
    /// Set by the app at launch (task 10). Without one, a call is only a screen with an "End" button.
    var callHandler: (any CallHandling)?
    /// Told the new catalog whenever it changes (a server answers, is added or removed), to share
    /// it with the widgets and App Intents.
    var onAgentsChanged: (([CatalogAgent]) -> Void)?
    /// Told when a one-way call's result becomes final, `true` when delivered. The app plays a haptic.
    var onCallResultFinished: ((Bool) -> Void)?
    /// Set by the push build at launch (task 12); `nil` means no push at all.
    var pushHandler: (any PushHandling)?
    /// Told the paired servers whenever the list changes (launch, pairing, removal), so the iPhone
    /// learns which servers are on this watch (`WatchLinkReceiver`, decision R10).
    var onServersChanged: (([Credentials]) -> Void)?

    private var activeCall: CallRequest?
    private var pairingTask: Task<Void, Never>?
    /// The first launch, shared by everyone who needs it (`launchIfNeeded()`).
    private var launchTask: Task<Void, Never>?
    /// The list last handed to `onServersChanged`.
    private var publishedServers: [Credentials]?

    private let pairing: any PairingService
    private let store: any ServerStore
    private let defaults: UserDefaults
    private let deviceName: String
    private let sleep: PairingClient.Sleep
    /// Asked before every call; `nil` (tests, previews) never blocks one.
    private let reachability: (any NetworkReachability)?
    /// Asks for the result of each one-way call.
    private let resultPoller: CallStatusPoller
    /// One-way calls without a final status, kept across launches.
    private let pendingResults: PendingResultStore
    /// Results the user closed with "Done" while still waiting: not reopened when the app comes
    /// back to the foreground, only by the next launch.
    private var dismissedResultIDs: Set<String> = []
    /// A tapped notification's call: opens before any pending result, once its server answered.
    private var requestedResult: PendingResult?

    init(
        pairing: any PairingService = PairingClient(),
        store: any ServerStore = KeychainServerStore(),
        defaults: UserDefaults = .standard,
        deviceName: String = AppModel.defaultDeviceName,
        reachability: (any NetworkReachability)? = nil,
        sleep: @escaping PairingClient.Sleep = { try await Task.sleep(for: $0) },
        savedCatalog: [CatalogAgent] = [],
        resultPoller: CallStatusPoller = CallStatusPoller(),
        pendingResults: PendingResultStore? = nil
    ) {
        self.pairing = pairing
        self.store = store
        self.defaults = defaults
        self.deviceName = deviceName
        self.sleep = sleep
        self.reachability = reachability
        self.resultPoller = resultPoller
        self.pendingResults = pendingResults ?? PendingResultStore(defaults: defaults)
        catalog = savedCatalog
        directoryURL = defaults.string(forKey: Self.directoryDefaultsKey).flatMap(ServerAddress.parse)
            ?? PairingClient.defaultDirectory
    }

    /// The agents of the servers that answered, in server order and then in each server's order.
    var agents: [AgentTarget] { servers.flatMap(\.agents) }
    var hasServers: Bool { !servers.isEmpty }
    /// Some server has not answered `GET /v1/me` yet.
    var isLoadingServers: Bool { servers.contains { $0.status == .loading } }
    /// The agents this build can call (decision W7), in grid order: what the "…" on Home offers.
    var callableAgents: [AgentTarget] { agents.filter { $0.agent.callType.isSupported } }
    var canCall: Bool {
        phase == .home && !callableAgents.isEmpty
    }

    /// The server `request` needs has not answered yet: the one of its agent, the first server when
    /// it names none (decision W17), or any server for a redial by name (the name must be unique).
    /// A shortcut waits only for that one, so a server that hangs does not hold up the others.
    func isLoadingServer(for request: PendingCall) -> Bool {
        if request.agentName != nil { return isLoadingServers }
        let entry: ServerEntry?
        if let ref = request.agent {
            entry = AgentRef(ref).flatMap { ref in servers.first { $0.id == ref.serverID } }
        } else {
            entry = servers.first
        }
        return entry?.status == .loading
    }
    /// A pairing request is running.
    var isBusy: Bool { pairingTask != nil }

    // MARK: - Launch

    /// Reads the servers from the Keychain, goes Home and asks every server for its agents.
    func launch() async {
        phase = .launching
        message = nil
        let stored: [Credentials]
        do {
            stored = try store.load()
        } catch let error as CredentialStoreError where error.isInteractionNotAllowed {
            // Locked before the first unlock: the item is fine, try again later.
            phase = .unavailable
            message = Message.locked
            return
        } catch CredentialStoreError.corruptedData {
            // The item is not a valid list: pairing again is the only way out.
            try? store.deleteAll()
            servers = []
            publishCatalog()
            publishServers()
            phase = .unpaired
            return
        } catch {
            // Any other Keychain failure may be transient: keep the item, offer "Retry".
            phase = .unavailable
            message = Message.keychain
            return
        }
        servers = stored.map { ServerEntry(credentials: $0, status: .loading) }
        publishCatalog()
        publishServers()
        guard !stored.isEmpty else {
            phase = .unpaired
            return
        }
        phase = .home
        await load(stored)
        resumePendingResult()
    }

    /// The first `launch()`, run once whoever asks first (the scene, or a message from the iPhone that
    /// woke the app); later callers wait for the same one.
    func launchIfNeeded() async {
        if let launchTask {
            await launchTask.value
            return
        }
        let task = Task { await launch() }
        launchTask = task
        await task.value
    }

    /// "Retry": reads the Keychain again after it failed, or asks again every server that is down.
    func retry() async {
        if phase == .unavailable {
            await launch()
        } else {
            await load(servers.filter(\.isUnavailable).map(\.credentials))
        }
    }

    /// Asks every server for its agents again ("Refresh watch" on the iPhone, decision R16).
    func reloadAgents() async {
        await load(servers.map(\.credentials))
    }

    /// "Retry" on the row of a server that is down.
    func retry(serverID: String) async {
        guard let entry = servers.first(where: { $0.id == serverID }), entry.isUnavailable else { return }
        await load([entry.credentials])
    }

    /// Asks every server in `list` at once; each one shows up as soon as it answers. They are marked
    /// `.loading` before the first suspension, so a second "Retry" meanwhile finds nothing to retry.
    private func load(_ list: [Credentials]) async {
        guard !list.isEmpty else { return }
        for credentials in list {
            if let index = servers.firstIndex(where: { $0.credentials == credentials }) {
                servers[index].status = .loading
            }
        }
        let pairing = pairing
        await withTaskGroup(of: (Credentials, Result<DeviceInfo, any Error>).self) { group in
            for credentials in list {
                group.addTask { (credentials, await Self.me(credentials, pairing: pairing)) }
            }
            for await (credentials, result) in group {
                apply(result, for: credentials)
            }
        }
    }

    /// An answer lands only on the same server with the same token: a server removed or paired
    /// again while the request was in flight is left alone.
    private func apply(_ result: Result<DeviceInfo, any Error>, for credentials: Credentials) {
        guard let index = servers.firstIndex(where: { $0.credentials == credentials }) else { return }
        switch result {
        case .success(let info):
            servers[index].status = .ready(info)
        case .failure(PairingError.unauthorized):
            // Review Focus 2: this token was revoked on the server; the other servers stay.
            let host = servers[index].host
            forget(credentials)
            message = Message.removed(host)
            return
        case .failure(let error):
            servers[index].status = .unavailable(Self.text(for: error))
        }
        publishCatalog()
    }

    private nonisolated static func me(
        _ credentials: Credentials, pairing: any PairingService
    ) async -> Result<DeviceInfo, any Error> {
        do {
            return .success(try await pairing.me(server: credentials.serverURL, token: credentials.token))
        } catch {
            return .failure(error)
        }
    }

    // MARK: - Pairing

    /// "Use server URL": `false` (and a message) unless `text` is `https://`, or `http://` to localhost.
    @discardableResult
    func useServerURL(_ text: String) -> Bool {
        guard let url = ServerAddress.parse(text) else {
            message = Message.invalidURL
            return false
        }
        customServerURL = url
        message = nil
        return true
    }

    /// Back to resolving codes with the directory.
    func useDirectory() {
        customServerURL = nil
        message = nil
    }

    /// Flow A (code resolved by the directory) or A' (code for `customServerURL`). A server in
    /// manual mode may answer with a pending request; then this continues as flow B.
    @discardableResult
    func pair(code: PairingCode) -> Task<Void, Never> {
        let custom = customServerURL
        let directory = directoryURL
        return startPairing { [self] in
            let server: URL
            if let custom {
                server = custom
            } else {
                do {
                    server = try await pairing.resolve(code: code, directory: directory)
                } catch PairingError.network(let code) where code != .cancelled {
                    throw LocalFailure.directoryUnreachable
                }
            }
            let result = try await pairing.pair(server: server, code: code, deviceName: deviceName)
            try await complete(result, server: server)
        }
    }

    /// Pairing sent by the iPhone (decision R10): flow A' with `server`, as if the code had been typed,
    /// without touching `customServerURL`. Runs on Home or the pairing screen only; anywhere else (a
    /// call, its result, another pairing) it fails with `LinkPairingError.busy` and changes nothing.
    /// A failure goes back to the screen it started on, with the reason in `message`.
    /// `onPending` runs as soon as the server asks for the owner's approval (with the request id);
    /// this then keeps waiting for it, like flow B.
    func pair(
        server: URL, code: PairingCode, onPending: (@MainActor (String) -> Void)? = nil
    ) async -> Result<Void, any Error> {
        guard pairingTask == nil, phase == .home || phase == .unpaired else { return .failure(LinkPairingError.busy) }
        let outcome = PairingOutcome()
        let task = startPairing(returningTo: phase) { [self] in
            do {
                let result = try await pairing.pair(server: server, code: code, deviceName: deviceName)
                if case .pending(let request) = result { onPending?(request.requestId) }
                try await complete(result, server: server)
            } catch {
                outcome.error = error
                throw error
            }
        }
        await task.value
        return outcome.error.map { .failure($0) } ?? .success(())
    }

    /// Flow B: asks `customServerURL` to pair without a code and waits for the owner's approval.
    @discardableResult
    func requestApproval() -> Task<Void, Never> {
        guard let server = customServerURL else {
            message = Message.serverURLNeeded
            return Task {}
        }
        return startPairing { [self] in
            let result = try await pairing.pair(server: server, code: nil, deviceName: deviceName)
            try await complete(result, server: server)
        }
    }

    /// "Cancel" while waiting: stops polling and goes back to the pairing screen.
    func cancelPairing() {
        pairingTask?.cancel()
    }

    /// Runs one pairing attempt; a second tap while one runs returns the running one. A failure goes
    /// back to `fallback` (the pairing screen, or Home for a pairing the iPhone sent).
    private func startPairing(
        returningTo fallback: AppPhase = .unpaired, _ work: @escaping @MainActor () async throws -> Void
    ) -> Task<Void, Never> {
        if let pairingTask { return pairingTask }
        phase = .pairing(requestId: nil)
        message = nil
        let task = Task { [self] in
            do {
                try await work()
            } catch {
                // A "Cancel" that already left the pairing screen (adding a server) keeps its screen.
                if case .pairing = phase {
                    phase = fallback
                    message = Task.isCancelled || Self.isCancellation(error) ? nil : Self.text(for: error)
                }
            }
            pairingTask = nil
        }
        pairingTask = task
        return task
    }

    private func complete(_ result: PairResult, server: URL) async throws {
        try Task.checkCancellation()
        let device: PairedDevice
        switch result {
        case .paired(let paired):
            device = paired
        case .pending(let request):
            device = try await waitForApproval(request, server: server)
        }
        try Task.checkCancellation()
        phase = .pairing(requestId: nil)
        let credentials = Credentials(serverURL: server, device: device)
        // The device exists on the server from here on: a late "Cancel" must not lose its token,
        // so `GET /v1/me` runs outside this cancellable task.
        let pairing = pairing
        let info = await Task { await Self.me(credentials, pairing: pairing) }.value
        try add(credentials, info: info)
    }

    /// Decision W2: the same URL and user as a listed server replaces it under the same local id
    /// (complications keep pointing to it) and revokes the old token without waiting. Another user
    /// on the same server is another entry. A server that does not answer is kept, with "Retry".
    private func add(_ credentials: Credentials, info: Result<DeviceInfo, any Error>) throws {
        var list = servers
        var replaced: Credentials?
        var added = credentials
        switch info {
        case .success(let info):
            if let index = list.firstIndex(where: { $0.isSameAccount(as: credentials.serverURL, info) }) {
                replaced = list[index].credentials
                added.id = list[index].id
                list[index] = ServerEntry(credentials: added, status: .ready(info))
            } else {
                list.append(ServerEntry(credentials: credentials, status: .ready(info)))
            }
        case .failure(PairingError.unauthorized):
            throw PairingError.unauthorized
        case .failure(let error):
            list.append(ServerEntry(credentials: credentials, status: .unavailable(Self.text(for: error))))
        }
        try store.save(list.map(\.credentials))
        servers = list
        if let replaced {
            let pairing = pairing
            Task { try? await pairing.unpair(server: replaced.serverURL, token: replaced.token) }
        }
        customServerURL = nil
        isAddingServer = false
        message = nil
        switch phase {
        case .unpaired, .pairing: phase = .home
        default: break
        }
        publishCatalog()
        publishServers()
        pushHandler?.serverAdded(added)
    }

    /// Polls every `PairingClient.pollInterval`. Shows only `requestId`: the poll token is a
    /// client secret and never leaves this function except in the request body.
    private func waitForApproval(_ request: PairingRequest, server: URL) async throws -> PairedDevice {
        phase = .pairing(requestId: request.requestId)
        while true {
            try await sleep(PairingClient.pollInterval)
            try Task.checkCancellation()
            switch try await pairing.poll(server: server, pollToken: request.pollToken) {
            case .pending:
                continue
            case .paired(let device):
                return device
            case .gone:
                throw LocalFailure.expired
            }
        }
    }

    // MARK: - Adding and removing servers

    /// "Add server" in Settings: the pairing screen, over the servers already paired.
    func addServer() {
        guard phase == .home else { return }
        isAddingServer = true
        customServerURL = nil
        message = nil
        phase = .unpaired
    }

    /// "Cancel" on the pairing screen while adding: stops a running request and goes back Home.
    func cancelAddServer() {
        guard isAddingServer else { return }
        pairingTask?.cancel()
        isAddingServer = false
        customServerURL = nil
        message = nil
        phase = hasServers ? .home : .unpaired
    }

    /// `DELETE /v1/me`, then forgets the server even if it could not be reached. Replaces 0.1.0's
    /// "Unpair". A second tap while the first runs does nothing.
    func removeServer(id: String) async {
        guard let entry = servers.first(where: { $0.id == id }), !removingServerIDs.contains(id) else { return }
        removingServerIDs.insert(id)
        defer { removingServerIDs.remove(id) }
        // The server forgets the push key while the token still works.
        await pushHandler?.serverWillBeRemoved(entry.credentials)
        var offline = false
        do {
            try await pairing.unpair(server: entry.credentials.serverURL, token: entry.credentials.token)
        } catch PairingError.unauthorized {
            // Already revoked on the server.
        } catch {
            offline = true
        }
        message = nil
        forget(entry.credentials)
        if offline, message == nil {
            message = Message.unpairedOffline
        }
    }

    /// Drops the server with these exact credentials (not one paired again since) from the list and
    /// the Keychain. With no server left, Home becomes the pairing screen.
    private func forget(_ credentials: Credentials) {
        let remaining = servers.filter { $0.credentials != credentials }
        guard remaining.count < servers.count else { return }
        servers = remaining
        do {
            try store.save(remaining.map(\.credentials))
        } catch {
            message = Message.keychain
        }
        pushHandler?.serverRemoved(credentials)
        publishCatalog()
        publishServers()
        if remaining.isEmpty, phase == .home {
            phase = .unpaired
        }
    }

    /// Hands the catalog to `onAgentsChanged` when it changed: the agents of every server that
    /// answered and, for one that did not (yet), what the last catalog had for it, so complications
    /// and shortcuts pointing there survive a launch without network. Removed servers drop out.
    /// Tells `onServersChanged` the paired servers when the list (not just a status) changed.
    private func publishServers() {
        let list = servers.map(\.credentials)
        guard list != publishedServers else { return }
        publishedServers = list
        onServersChanged?(list)
    }

    private func publishCatalog() {
        let previous = Dictionary(grouping: catalog, by: \.ref.serverID)
        let next = servers.flatMap { entry -> [CatalogAgent] in
            if case .ready = entry.status { return entry.agents.map(\.catalogEntry) }
            return previous[entry.id] ?? []
        }
        guard next != catalog else { return }
        catalog = next
        onAgentsChanged?(next)
    }

    // MARK: - Settings

    /// Saves the pairing directory; `false` (and a message) for an address `ServerAddress` rejects.
    @discardableResult
    func setDirectory(_ text: String) -> Bool {
        guard let url = ServerAddress.parse(text) else {
            message = Message.invalidURL
            return false
        }
        directoryURL = url
        defaults.set(url.absoluteString, forKey: Self.directoryDefaultsKey)
        message = nil
        return true
    }

    func resetDirectory() {
        directoryURL = PairingClient.defaultDirectory
        defaults.removeObject(forKey: Self.directoryDefaultsKey)
    }

    /// `"0.1.0 (1)"`.
    static func version(of bundle: Bundle = .main) -> String {
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"
        return "\(version) (\(build))"
    }

    // MARK: - Call (wired by tasks 8 to 10)

    /// A tap on an agent, or a choice on its call options screen. Only from `.home`, for an agent
    /// this build can call (decision W7), and only with a network path: without one the call would
    /// fail, and watchOS 26's "Call Failed" alert crashed the system UI.
    func startCall(_ target: AgentTarget, turnEnd: TurnEnd? = nil) {
        guard phase == .home else { return }
        // The target may come from a screen drawn before the last `/v1/me`: call what is listed now.
        guard let entry = servers.first(where: { $0.id == target.serverID }),
              let current = entry.agents.first(where: { $0.agent.id == target.agent.id })
        else {
            message = Message.agentNotFound
            return
        }
        guard current.agent.callType.isSupported else {
            message = Message.unsupportedAgent
            return
        }
        guard reachability?.hasNetworkPath != false else {
            message = Message.noConnection
            return
        }
        let request = CallRequest(credentials: entry.credentials, target: current, turnEnd: turnEnd)
        activeCall = request
        message = nil
        callActivity = .connecting
        phase = .inCall(current)
        callHandler?.startCall(request)
    }

    /// A shortcut, the complication or the control. `ref` is the text of an `AgentRef`; `nil` calls
    /// the first agent this build can call on the first server (decision W17): with that server down
    /// or still loading, it says so and calls nothing, never an agent of the next server. An agent
    /// that is gone never turns into a call to another one (decision W4).
    func startCall(agent ref: String? = nil, turnEnd: TurnEnd? = nil) {
        guard phase == .home else { return }
        guard let ref else {
            guard let entry = servers.first else { return }
            guard entry.isReady else {
                message = Message.cantReach(entry.host)
                return
            }
            if let first = entry.agents.first(where: { $0.agent.callType.isSupported }) {
                startCall(first, turnEnd: turnEnd)
            }
            return
        }
        guard let agentRef = AgentRef(ref), let entry = servers.first(where: { $0.id == agentRef.serverID }) else {
            message = Message.agentNotFound
            return
        }
        // Its server is down: the agent may well still exist there.
        guard case .ready = entry.status else {
            message = Message.cantReach(entry.host)
            return
        }
        guard let target = entry.agents.first(where: { $0.ref == agentRef }) else {
            message = Message.agentNotFound
            return
        }
        startCall(target, turnEnd: turnEnd)
    }

    /// The system's redial (decision W19). Its CallKit handle is the agent's display name, so only
    /// the one agent with exactly that name is called; a name no agent or several agents have says
    /// "Agent not found." (never a guess). An empty handle calls the first agent, as 0.1.0 did.
    /// Uniqueness is only known when every server answered: a server that is down or still loading
    /// may hold a second agent with the same name, so then it says "Can't reach <host>." and calls
    /// nobody (decision W4).
    func startCall(redialing name: String) {
        guard phase == .home else { return }
        guard !name.isEmpty else {
            startCall(agent: nil)
            return
        }
        if let unavailable = servers.first(where: { !$0.isReady }) {
            message = Message.cantReach(unavailable.host)
            return
        }
        let named = agents.filter { $0.agent.displayName == name }
        guard named.count == 1, let target = named.first else {
            message = Message.agentNotFound
            return
        }
        startCall(target)
    }

    /// The "End" button on the call screen.
    func endCall() {
        guard case .inCall = phase else { return }
        if let callHandler {
            callHandler.endCall()
        } else {
            callDidEnd(.normal)
        }
    }

    /// Reported by the call layer when a call ends, whoever ended it. `callID` comes from
    /// `session.ready` of a one-way call; a conversation, or a call that never got ready, has none.
    func callDidEnd(_ reason: CallEndReason, callID: String? = nil) {
        guard case .inCall = phase else { return }
        // Decision W9: hanging up or a dropped connection (the server counts it as hanging up) both
        // leave a recording on its way to the agent, so the result screen follows.
        if let call = activeCall, call.target.agent.callType.isOneWay, let callID,
           reason == .normal || reason == .connectionLost {
            showResult(of: call, callID: callID)
            return
        }
        switch reason {
        case .unauthorized:
            // 4401: the token of this call's server was revoked; the other servers are not affected.
            if let call = activeCall {
                forget(call.credentials)
                message = Message.removed(call.target.serverHost)
            }
        case .connectionLost:
            message = Message.connectionLost
        case .serverFatal(let code?):
            message = "Call failed (\(code.wireValue))."
        case .serverFatal(nil):
            message = "Call failed."
        case .normal:
            // A one-way call ended before `session.ready` (no call id): nothing was recorded.
            message = activeCall?.target.agent.callType.isOneWay == true ? Message.nothingSent : nil
        }
        returnHome()
    }

    /// Reported by the call layer when the call could not start or go on for a local reason
    /// (CallKit refused it, the audio never activated, no microphone). Back to Home with `message`.
    func callDidFail(message: String) {
        guard case .inCall = phase else { return }
        self.message = message
        returnHome()
    }

    /// Reported by the call layer as the server's events arrive.
    func callActivityDidChange(_ activity: CallActivity) {
        guard case .inCall = phase else { return }
        callActivity = activity
    }

    private func returnHome() {
        activeCall = nil
        phase = hasServers ? .home : .unpaired
    }

    // MARK: - Result of a one-way call

    /// "Done" on the result screen: stops asking and goes back to the grid. A message set while the
    /// screen was up (the server was removed by a `401`) stays: it says why the grid changed.
    /// A result still unknown stays pending: the next launch picks it up.
    func dismissResult() {
        guard phase == .callResult else { return }
        if let callResult, !callResult.isOver {
            dismissedResultIDs.insert(callResult.callID)
        }
        callResult?.stop()
        callResult = nil
        returnHome()
    }

    /// The app came to the foreground: a result still waiting asks again (Review Focus 3); with
    /// none open, a pending one from an earlier launch comes back.
    func sceneDidBecomeActive() {
        callResult?.appBecameActive()
        resumePendingResult()
    }

    /// A push says this call is finished. When its result is on screen, it ends the wait at once
    /// and the answer is `true` (no banner needed); any other call changes nothing.
    @discardableResult
    func pushArrived(serverID: String, callID: String) -> Bool {
        guard let callResult, callResult.callID == callID, callResult.target.serverID == serverID else { return false }
        callResult.pushArrived()
        return true
    }

    /// A tapped notification of a finished call: opens its result the way a pending one from an
    /// earlier launch opens (`serverID` is the push's tag, the server's local id). A result of
    /// another call on screen gives way; during a call, or before the servers are read, it waits.
    func openResult(serverID: String, callID: String, agentID: String?) {
        if pushArrived(serverID: serverID, callID: callID) { return }
        dismissedResultIDs.remove(callID)
        requestedResult = PendingResult(callID: callID, serverID: serverID, agentID: agentID, startedAt: Date())
        if phase == .callResult {
            callResult?.stop()
            callResult = nil
            returnHome()
        }
        resumePendingResult()
    }

    private func showResult(of call: CallRequest, callID: String) {
        pendingResults.add(PendingResult(
            callID: callID, serverID: call.credentials.id, agentID: call.target.agent.id, startedAt: Date()))
        activeCall = nil
        message = nil
        present(
            CallResultModel(
                target: call.target, callID: callID, credentials: call.credentials, pairing: pairing,
                poller: resultPoller),
            credentials: call.credentials)
        pushHandler?.oneWayResultShown()
    }

    /// Opens the newest pending result, when the app is at Home with no result open. An entry of a
    /// server that is no longer paired is dropped. A server that has not answered yet waits:
    /// `launch()` tries again once every server did.
    /// A tapped notification's call (`openResult`) comes first.
    private func resumePendingResult() {
        if let requested = requestedResult, phase == .home, callResult == nil {
            guard let entry = servers.first(where: { $0.id == requested.serverID }) else {
                // Not a server of this watch (any more).
                requestedResult = nil
                return resumePendingResult()
            }
            guard entry.status != .loading else { return }
            requestedResult = nil
            return presentPending(requested, on: entry)
        }
        while phase == .home, callResult == nil, let pending = pendingResults.latest() {
            guard !dismissedResultIDs.contains(pending.callID) else { return }
            guard let entry = servers.first(where: { $0.id == pending.serverID }) else {
                pendingResults.remove(callID: pending.callID)
                continue
            }
            guard entry.status != .loading else { return }
            presentPending(pending, on: entry)
            return
        }
    }

    private func presentPending(_ pending: PendingResult, on entry: ServerEntry) {
        let agent = entry.agents.first { $0.agent.id == pending.agentID }?.agent
            ?? Agent(id: pending.agentID ?? "", slug: "", displayName: Message.unknownAgent, callType: .oneShot)
        present(
            CallResultModel(
                target: AgentTarget(serverID: entry.id, serverHost: entry.host, agent: agent),
                callID: pending.callID, credentials: entry.credentials, pairing: pairing,
                poller: resultPoller),
            credentials: entry.credentials)
    }

    private func present(_ result: CallResultModel, credentials: Credentials) {
        let callID = result.callID
        let host = result.target.serverHost
        result.onFinished = { [weak self] delivered in self?.onCallResultFinished?(delivered) }
        result.onSettled = { [weak self] in self?.pendingResults.remove(callID: callID) }
        // The screen stays (it says "Result unavailable"); "Done" then finds the server gone.
        result.onUnauthorized = { [weak self] in
            guard let self else { return }
            forget(credentials)
            message = Message.removed(host)
        }
        callResult = result
        phase = .callResult
        result.start()
    }

    // MARK: - Messages

    /// The failure of one `pair(server:code:)`, kept by that call only.
    @MainActor
    private final class PairingOutcome {
        var error: (any Error)?
    }

    /// Why `pair(server:code:)` did not start.
    enum LinkPairingError: Error, Equatable {
        /// A call, its result or another pairing is on screen.
        case busy
    }

    private enum LocalFailure: Error {
        case expired
        case directoryUnreachable
    }

    /// `PairingClient` can surface a cancelled retry sleep as a bare `CancellationError`, and a
    /// cancelled request as `PairingError.network(.cancelled)`: neither is worth a message.
    static func isCancellation(_ error: any Error) -> Bool {
        error is CancellationError || error as? PairingError == .network(.cancelled)
    }

    static func text(for error: any Error) -> String {
        switch error {
        case LocalFailure.expired: Message.expired
        case LocalFailure.directoryUnreachable: Message.directoryUnreachable
        case PairingError.invalidCode: Message.invalidCode
        case PairingError.codeNotFound: Message.codeNotFound
        case PairingError.insecureServerURL: Message.insecureServer
        case PairingError.rateLimited: Message.rateLimited
        case PairingError.network: Message.unreachable
        case PairingError.unauthorized: Message.revoked
        case is CredentialStoreError: Message.keychain
        default: Message.unexpected
        }
    }
}

private extension ServerEntry {
    /// Decision W2: the same server and the same account (`user` is `nil` on both for servers
    /// before 0.5.0, which have a single owner). Only a server that answered can be compared.
    func isSameAccount(as url: URL, _ info: DeviceInfo) -> Bool {
        guard credentials.serverURL == url, case .ready(let known) = status else { return false }
        return known.user?.id == info.user?.id
    }
}
