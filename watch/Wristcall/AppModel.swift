import Foundation
import Observation
import WristcallKit

/// Which screen the app shows.
enum AppPhase: Equatable {
    /// Reading the Keychain and loading the profiles.
    case launching
    /// No credentials: the pairing screen.
    case unpaired
    /// A pairing request is running. `requestId` (4 digits) is set while waiting for the
    /// owner to run `wristcall devices approve <requestId>` (flow B).
    case pairing(requestId: String?)
    /// Paired; `GET /v1/me` answered.
    case ready(DeviceInfo)
    /// Paired, but the profiles could not be loaded (server unreachable, Keychain still locked).
    case unavailable
    /// A call is active with this profile (`nil`: the server's default).
    case inCall(Profile?)
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

    var label: String {
        switch self {
        case .connecting: "Connecting…"
        case .listening: "Listening"
        case .thinking: "Thinking…"
        case .agentSpeaking: "Speaking"
        }
    }
}

/// What the call layer (tasks 8 to 10) needs to open a call.
struct CallRequest: Sendable, Equatable {
    let credentials: Credentials
    /// The profile to ask for in `session.start`; its `displayName` is the CallKit caller name.
    let profile: Profile?
    /// How the user's turn ends in this call (`session.start` `turn_end`).
    var turnEnd: TurnEnd = .auto
}

/// Implemented by the `CallCoordinator` (task 10). `AppModel` keeps a strong reference;
/// the handler must hold the model weakly.
@MainActor
protocol CallHandling: AnyObject {
    /// Start a CallKit call for `request`. Report its end, whatever the cause, with `AppModel.callDidEnd(_:)`.
    func startCall(_ request: CallRequest)
    /// The user tapped "End" in the app. Report the end with `AppModel.callDidEnd(_:)`.
    func endCall()
}

/// State and actions behind every screen: credentials, pairing (flows A, A' and B), unpairing,
/// settings and the entry point of a call.
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
    private(set) var isUnpairing = false
    /// What the call screen shows; meaningful only in `.inCall`.
    private(set) var callActivity: CallActivity = .connecting
    /// Set by the app at launch (task 10). Without one, a call is only a screen with an "End" button.
    var callHandler: (any CallHandling)?

    private var credentials: Credentials?
    private var deviceInfo: DeviceInfo?
    private var pairingTask: Task<Void, Never>?

    private let pairing: any PairingService
    private let store: any CredentialStore
    private let defaults: UserDefaults
    private let deviceName: String
    private let sleep: PairingClient.Sleep
    /// Asked before every call; `nil` (tests, previews) never blocks one.
    private let reachability: (any NetworkReachability)?

    init(
        pairing: any PairingService = PairingClient(),
        store: any CredentialStore = KeychainCredentialStore(),
        defaults: UserDefaults = .standard,
        deviceName: String = AppModel.defaultDeviceName,
        reachability: (any NetworkReachability)? = nil,
        sleep: @escaping PairingClient.Sleep = { try await Task.sleep(for: $0) }
    ) {
        self.pairing = pairing
        self.store = store
        self.defaults = defaults
        self.deviceName = deviceName
        self.sleep = sleep
        self.reachability = reachability
        directoryURL = defaults.string(forKey: Self.directoryDefaultsKey).flatMap(ServerAddress.parse)
            ?? PairingClient.defaultDirectory
    }

    /// The server this watch is paired with.
    var serverURL: URL? { credentials?.serverURL }
    /// The profile a call uses: the first one of `GET /v1/me` (no profile picker in the MVP).
    var profile: Profile? { deviceInfo?.profiles.first }
    var canCall: Bool {
        if case .ready = phase { true } else { false }
    }
    /// A pairing request is running.
    var isBusy: Bool { pairingTask != nil }

    // MARK: - Launch

    /// Loads the credentials from the Keychain and the profiles from the server.
    func launch() async {
        phase = .launching
        message = nil
        let stored: Credentials?
        do {
            stored = try store.load()
        } catch let error as CredentialStoreError where error.isInteractionNotAllowed {
            // Locked before the first unlock: the item is fine, try again later.
            phase = .unavailable
            message = Message.locked
            return
        } catch CredentialStoreError.corruptedData {
            // The item is not valid credentials: pairing again is the only way out.
            try? store.delete()
            phase = .unpaired
            return
        } catch {
            // Any other Keychain failure may be transient: keep the item, offer "Retry".
            phase = .unavailable
            message = Message.keychain
            return
        }
        guard let stored else {
            phase = .unpaired
            return
        }
        credentials = stored
        await loadProfiles()
    }

    /// "Retry" on the Home screen.
    func retry() async {
        if credentials == nil {
            await launch()
        } else {
            phase = .launching
            await loadProfiles()
        }
    }

    private func loadProfiles() async {
        guard let credentials else {
            phase = .unpaired
            return
        }
        do {
            let info = try await pairing.me(server: credentials.serverURL, token: credentials.token)
            deviceInfo = info
            phase = .ready(info)
            message = nil
        } catch PairingError.unauthorized {
            // Review Focus 3: the token was revoked on the server.
            forgetCredentials()
            message = Message.revoked
        } catch {
            phase = .unavailable
            message = Self.text(for: error)
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

    /// Runs one pairing attempt; a second tap while one runs returns the running one.
    private func startPairing(_ work: @escaping @MainActor () async throws -> Void) -> Task<Void, Never> {
        if let pairingTask { return pairingTask }
        phase = .pairing(requestId: nil)
        message = nil
        let task = Task { [self] in
            do {
                try await work()
            } catch {
                phase = .unpaired
                message = Task.isCancelled || Self.isCancellation(error) ? nil : Self.text(for: error)
            }
            pairingTask = nil
        }
        pairingTask = task
        return task
    }

    private func complete(_ result: PairResult, server: URL) async throws {
        let device: PairedDevice
        switch result {
        case .paired(let paired):
            device = paired
        case .pending(let request):
            device = try await waitForApproval(request, server: server)
        }
        try Task.checkCancellation()
        let credentials = Credentials(serverURL: server, device: device)
        try store.save(credentials)
        self.credentials = credentials
        customServerURL = nil
        phase = .launching
        await loadProfiles()
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

    // MARK: - Unpair

    /// `DELETE /v1/me`, then clears the Keychain even if the server could not be reached.
    func unpair() async {
        guard let credentials else {
            forgetCredentials()
            return
        }
        isUnpairing = true
        defer { isUnpairing = false }
        var offline = false
        do {
            try await pairing.unpair(server: credentials.serverURL, token: credentials.token)
        } catch PairingError.unauthorized {
            // Already revoked on the server.
        } catch {
            offline = true
        }
        forgetCredentials()
        if offline, message == nil {
            message = Message.unpairedOffline
        }
    }

    private func forgetCredentials() {
        message = nil
        do {
            try store.delete()
        } catch {
            message = Message.keychain
        }
        credentials = nil
        deviceInfo = nil
        customServerURL = nil
        phase = .unpaired
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

    /// The "Call" button (auto), a choice on the call options screen, a shortcut, the complication,
    /// the control or the system's redial. Only from `.ready`, and only with a network path: without
    /// one the call would fail, and watchOS 26's "Call Failed" alert crashed the system UI.
    func startCall(turnEnd: TurnEnd = .auto) {
        guard case .ready(let info) = phase, let credentials else { return }
        guard reachability?.isSatisfied != false else {
            message = Message.noConnection
            return
        }
        let request = CallRequest(credentials: credentials, profile: info.profiles.first, turnEnd: turnEnd)
        message = nil
        callActivity = .connecting
        phase = .inCall(request.profile)
        callHandler?.startCall(request)
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

    /// Reported by the call layer when a call ends, whoever ended it.
    func callDidEnd(_ reason: CallEndReason) {
        guard case .inCall = phase else { return }
        switch reason {
        case .unauthorized:
            forgetCredentials()
            message = Message.revoked
            return
        case .connectionLost:
            message = Message.connectionLost
        case .serverFatal(let code?):
            message = "Call failed (\(code.wireValue))."
        case .serverFatal(nil):
            message = "Call failed."
        case .normal:
            message = nil
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
        if let deviceInfo {
            phase = .ready(deviceInfo)
        } else {
            phase = .unavailable
        }
    }

    // MARK: - Messages

    private enum LocalFailure: Error {
        case expired
        case directoryUnreachable
    }

    /// `PairingClient` can surface a cancelled retry sleep as a bare `CancellationError`, and a
    /// cancelled request as `PairingError.network(.cancelled)`: neither is worth a message.
    private static func isCancellation(_ error: any Error) -> Bool {
        error is CancellationError || error as? PairingError == .network(.cancelled)
    }

    private static func text(for error: any Error) -> String {
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
