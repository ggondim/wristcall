import Foundation
import os
import UserNotifications
import WatchKit
import WristcallKit

/// The push relay routes `PushCoordinator` uses (`PushRelayClient`), so tests can play the relay.
protocol PushRelaying: Sendable {
    func register(
        deviceToken: Data, topic: String, environment: PushEnvironment, label: String, tag: String
    ) async throws -> String
    func isRegistered(pushKey: String) async throws -> Bool
    func unregister(pushKey: String) async throws
}

extension PushRelayClient: PushRelaying {}

/// The push routes of one server `PushCoordinator` uses (`ServerPushClient`), so tests can play it.
protocol ServerPushing: Sendable {
    func health() async throws -> ServerHealth
    func setPushKey(_ key: String) async throws
    func clearPushKey() async throws
}

extension ServerPushClient: ServerPushing {}

// `StoredPushKey`, `PushKeyStore` and `KeychainPushKeyStore` live in WristcallKit (the phone uses them too).

/// Push notifications for the results of one-way calls (decisions R14, R17, R18, R20), compiled in
/// every build but wired by the app only in the push build (`WRISTCALL_PUSH`).
///
/// The watch registers its APNs token at the app's own relay (never one a server announces) once per
/// paired server, with the server's host as label and its local id as tag, and hands the push key to
/// that server. Every activation checks the keys again: a key the relay forgot is registered again,
/// and the server gets it again (`PUT /v1/push` is idempotent).
@MainActor
final class PushCoordinator: NSObject, PushHandling, UNUserNotificationCenterDelegate {
    /// Holds the coordinator strongly (`AppModel.pushHandler`), so this one is weak.
    weak var model: AppModel?

    /// `nil`: push is off (no relay in this build).
    private let relayURL: URL?
    private let relay: (any PushRelaying)?
    private let topic: String
    private let environment: PushEnvironment
    private let keys: any PushKeyStore
    private let makeServer: @Sendable (Credentials) -> any ServerPushing
    private let requestAuthorization: @MainActor () async -> Void
    /// The APNs device token of this launch; nothing is registered before it arrives.
    private(set) var deviceToken: Data?
    private var syncTask: Task<Void, Never>?
    /// Something asked for a sync while one ran: run once more when it ends.
    private var needsSync = false
    private var askedForAuthorization = false
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "push")

    init(
        relayURL: URL?,
        topic: String,
        environment: PushEnvironment,
        keys: any PushKeyStore = KeychainPushKeyStore(),
        makeRelay: (URL) -> any PushRelaying = { PushRelayClient(relayURL: $0) },
        makeServer: @escaping @Sendable (Credentials) -> any ServerPushing = { ServerPushClient(credentials: $0) },
        requestAuthorization: @escaping @MainActor () async -> Void = PushCoordinator.requestNotificationAuthorization
    ) {
        self.relayURL = relayURL
        relay = relayURL.map(makeRelay)
        self.topic = topic
        self.environment = environment
        self.keys = keys
        self.makeServer = makeServer
        self.requestAuthorization = requestAuthorization
    }

    /// The relay and APNs environment of this build, from `Info.plist` (set by `Config/Push.xcconfig`).
    convenience init(bundle: Bundle = .main) {
        self.init(
            relayURL: Self.relayURL(fromInfoValue: bundle.object(forInfoDictionaryKey: "WristcallRelayURL") as? String),
            topic: bundle.bundleIdentifier ?? "",
            environment: (bundle.object(forInfoDictionaryKey: "WristcallPushEnvironment") as? String)
                .flatMap(PushEnvironment.init(rawValue:)) ?? .production)
    }

    /// Empty in the default builds: push off.
    nonisolated static func relayURL(fromInfoValue value: String?) -> URL? {
        guard let value, !value.isEmpty, let url = URL(string: value), url.host() != nil else { return nil }
        return url
    }

    var isEnabled: Bool { relay != nil }

    // MARK: - Registration

    /// At launch, once the servers are read: asks APNs for a device token. In a Debug build,
    /// `-WCFakeAPNsToken <hex>` stands in for it (the watch simulator gets none).
    func start(arguments: [String] = ProcessInfo.processInfo.arguments) {
        guard isEnabled else {
            Self.log.notice("push off: no relay in this build")
            return
        }
        #if DEBUG
        if let index = arguments.firstIndex(of: "-WCFakeAPNsToken"), arguments.indices.contains(index + 1),
           let token = Data(hex: arguments[index + 1]) {
            didRegister(token: token)
            return
        }
        #endif
        #if WRISTCALL_PUSH
        WKApplication.shared().registerForRemoteNotifications()
        #endif
    }

    /// APNs answered with this launch's device token.
    @discardableResult
    func didRegister(token: Data) -> Task<Void, Never>? {
        deviceToken = token
        return sync()
    }

    /// Every activation checks the keys of every server again (R20).
    @discardableResult
    func sceneDidBecomeActive() -> Task<Void, Never>? {
        sync()
    }

    /// Registers (or checks) the key of every paired server. A sync asked for while one runs makes
    /// that one run again; the returned task ends when nothing is left to do.
    @discardableResult
    func sync() -> Task<Void, Never>? {
        guard isEnabled, deviceToken != nil else { return nil }
        if let syncTask {
            needsSync = true
            return syncTask
        }
        let task = Task { [weak self] () -> Void in
            await self?.runSyncs()
        }
        syncTask = task
        return task
    }

    private func runSyncs() async {
        repeat {
            needsSync = false
            for credentials in model?.servers.map(\.credentials) ?? [] {
                guard let token = deviceToken else { break }
                await sync(credentials, token: token)
            }
        } while needsSync
        syncTask = nil
    }

    /// One server: a failure here is logged (status only, never a key or token) and the next
    /// server goes on.
    private func sync(_ credentials: Credentials, token: Data) async {
        guard let relay, let relayURL else { return }
        let server = makeServer(credentials)
        do {
            let health = try await server.health()
            guard let announced = health.relay else {
                Self.log.notice("push: server has push off, skipped")
                return
            }
            // R17: the app's relay only. A server naming another one would get the APNs token there.
            guard Self.sameRelay(announced, relayURL) else {
                Self.log.notice("push: relay mismatch, server skipped")
                return
            }
            let tokenHex = token.hex
            var key: String?
            if let stored = try? keys.load(serverID: credentials.id) {
                if stored.deviceToken != tokenHex {
                    // A new APNs token: the old key would push to a dead token.
                    try? await relay.unregister(pushKey: stored.pushKey)
                    try? keys.delete(serverID: credentials.id)
                } else if try await relay.isRegistered(pushKey: stored.pushKey) {
                    key = stored.pushKey
                } else {
                    try? keys.delete(serverID: credentials.id)
                }
            }
            if key == nil {
                let new = try await relay.register(
                    deviceToken: token, topic: topic, environment: environment,
                    label: Self.registrationLabel(for: credentials.serverURL), tag: credentials.id)
                // The server may have been removed while the relay answered.
                guard model?.servers.contains(where: { $0.id == credentials.id }) == true else {
                    try? await relay.unregister(pushKey: new)
                    return
                }
                do {
                    try keys.save(StoredPushKey(pushKey: new, deviceToken: tokenHex), serverID: credentials.id)
                } catch {
                    // Without it the next sync would register yet another key.
                    try? await relay.unregister(pushKey: new)
                    throw error
                }
                key = new
            }
            if let key {
                try await server.setPushKey(key)
                Self.log.notice("push: key handed to the server")
            }
        } catch PairingError.notFound {
            // `404 not_configured` from the relay or the server: no push there, and nothing to tell.
            Self.log.notice("push: not configured, server skipped")
        } catch {
            Self.log.error("push: sync failed: \(Self.describe(error), privacy: .public)")
        }
    }

    // MARK: - PushHandling

    func serverAdded(_ credentials: Credentials) {
        sync()
    }

    /// Before `DELETE /v1/me`, while the server still takes the token: it forgets the push key.
    func serverWillBeRemoved(_ credentials: Credentials) async {
        guard isEnabled, (try? keys.load(serverID: credentials.id)) != nil else { return }
        do {
            try await makeServer(credentials).clearPushKey()
        } catch {
            Self.log.error("push: clearing the key failed: \(Self.describe(error), privacy: .public)")
        }
    }

    func serverRemoved(_ credentials: Credentials) {
        Task { await forgetKey(of: credentials) }
    }

    /// The server is gone: drops its key at the relay (best effort), then on the watch.
    func forgetKey(of credentials: Credentials) async {
        guard let stored = try? keys.load(serverID: credentials.id) else { return }
        if let relay {
            do {
                try await relay.unregister(pushKey: stored.pushKey)
            } catch {
                Self.log.error("push: unregister failed: \(Self.describe(error), privacy: .public)")
            }
        }
        try? keys.delete(serverID: credentials.id)
    }

    /// Notifications are asked for at the first one-way call, when the user sees why, not at the
    /// first launch.
    func oneWayResultShown() {
        guard isEnabled, !askedForAuthorization else { return }
        askedForAuthorization = true
        let requestAuthorization = requestAuthorization
        Task { await requestAuthorization() }
    }

    static func requestNotificationAuthorization() async {
        #if WRISTCALL_PUSH
        let center = UNUserNotificationCenter.current()
        guard await center.notificationSettings().authorizationStatus == .notDetermined else { return }
        do {
            _ = try await center.requestAuthorization(options: [.alert, .sound])
        } catch {
            log.error("push: authorization request failed")
        }
        #endif
    }

    // MARK: - Notifications

    /// A notification arrived with the app in the foreground. The result on screen ends its wait
    /// (the check shows without waiting for the next poll) and needs no banner; anything else shows one.
    func willPresent(_ message: PushMessage?) -> UNNotificationPresentationOptions {
        if case .callFinished(let tag, let callID, _, _, _) = message,
           model?.pushArrived(serverID: tag, callID: callID) == true {
            Self.log.notice("push: call.finished for the result on screen")
            return []
        }
        Self.log.notice("push: notification in the foreground, banner")
        return [.banner, .list, .sound]
    }

    /// The user tapped a notification: a finished call opens its result.
    func didReceive(_ message: PushMessage?) {
        guard case .callFinished(let tag, let callID, _, _, let agentID) = message else { return }
        Self.log.notice("push: call.finished tapped, opening its result")
        model?.openResult(serverID: tag, callID: callID, agentID: agentID)
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        let message = PushMessage(userInfo: notification.request.content.userInfo)
        return await willPresent(message)
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else { return }
        let message = PushMessage(userInfo: response.notification.request.content.userInfo)
        await didReceive(message)
    }

    // MARK: - Helpers

    /// The label the relay shows under every push of a registration: the server's host, cut to the relay's
    /// 64 characters (it refuses a longer one).
    static func registrationLabel(for serverURL: URL) -> String {
        guard let host = serverURL.host(), !host.isEmpty else { return "wristcall" }
        return String(String.UnicodeScalarView(host.unicodeScalars.prefix(64)))
    }

    /// `https://Cloud.example.com:443/` and `https://cloud.example.com` are the same relay: scheme and host
    /// lowercased, default port dropped, trailing slash removed. The path keeps its case.
    static func sameRelay(_ lhs: URL, _ rhs: URL) -> Bool {
        func normalized(_ url: URL) -> String? {
            guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
                  let scheme = parts.scheme?.lowercased(), let host = parts.host?.lowercased(),
                  parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil
            else { return nil }
            let defaultPort = ["http": 80, "https": 443][scheme]
            let port = parts.port.flatMap { $0 == defaultPort ? nil : ":\($0)" } ?? ""
            var path = parts.percentEncodedPath
            while path.hasSuffix("/") { path.removeLast() }
            return "\(scheme)://\(host)\(port)\(path)"
        }
        guard let left = normalized(lhs), let right = normalized(rhs) else { return false }
        return left == right
    }

    // MARK: - Private

    /// What may go to the log about a failure: a status or an error code, never a URL or a secret.
    private static func describe(_ error: any Error) -> String {
        switch error {
        case PairingError.unexpectedStatus(let status): "status \(status)"
        case PairingError.network(let code): "network \(code.rawValue)"
        case PairingError.unauthorized: "unauthorized"
        case PairingError.invalidRequest: "invalid request"
        case PairingError.rateLimited: "rate limited"
        case PairingError.malformedResponse: "malformed response"
        case let error as CredentialStoreError: "keychain \(error)"
        default: "other"
        }
    }
}

private extension Data {
    var hex: String { map { String(format: "%02x", $0) }.joined() }

    /// `nil` unless `hex` is an even number of hex digits.
    init?(hex: String) {
        guard hex.count.isMultiple(of: 2), !hex.isEmpty else { return nil }
        var bytes: [UInt8] = []
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }
}
