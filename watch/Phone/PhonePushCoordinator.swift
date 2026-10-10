import Foundation
import os
import UIKit
import UserNotifications
import WristcallKit

/// The push relay routes `PhonePushCoordinator` uses (`PushRelayClient`), so tests can play the relay.
protocol PhonePushRelaying: Sendable {
    func register(
        deviceToken: Data, topic: String, environment: PushEnvironment, label: String, tag: String, events: [String]
    ) async throws -> String
    func isRegistered(pushKey: String) async throws -> Bool
    func unregister(pushKey: String) async throws
}

extension PushRelayClient: PhonePushRelaying {}

/// Push notifications for device approvals (decision R9), the same design as the watch's `PushCoordinator`
/// (E6, R17, R18, R20). Compiled in every build so the tests run; the app creates it only in the push build
/// (`WRISTCALL_PUSH`), and only that build asks APNs for a token.
///
/// The iPhone registers its APNs token at the app's own relay (never one a server announces), anonymously,
/// once per server with an account, for `device.approval` alone, with the server's name as label and its
/// local id as tag; the key goes to that server with the personal token (`PUT /v1/push`). Keys live in the
/// Keychain (`push.<server id>` in the phone's service). Every activation checks them again: a key the relay
/// forgot is registered again, and a new APNs token replaces the old keys. All of it runs one step at a time.
@MainActor
final class PhonePushCoordinator {
    static let events = ["device.approval"]

    private let state: AppState
    private let relayURL: URL
    private let relay: any PhonePushRelaying
    private let environment: PushEnvironment
    private let topic: String
    private let keys: any PushKeyStore
    private let requestAuthorization: @MainActor () async -> Void
    /// The APNs device token of this launch; nothing is registered before it arrives.
    private(set) var deviceToken: Data?
    private var askedForAuthorization = false
    /// The permission prompt, apart from the queue (the user may take a while to answer).
    private var authorization: Task<Void, Never>?
    /// After the account was deleted, its keys stay undone until the next launch.
    private var stopped = false
    /// The last queued step; each one waits for the one before.
    private var chain: Task<Void, Never>?
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "push")

    init(
        state: AppState,
        relayURL: URL,
        relay: any PhonePushRelaying,
        environment: PushEnvironment,
        topic: String,
        keys: any PushKeyStore = KeychainPushKeyStore(service: KeychainManagedServerStore.defaultService),
        requestAuthorization: @escaping @MainActor () async -> Void = PhonePushCoordinator.requestNotificationAuthorization
    ) {
        self.state = state
        self.relayURL = relayURL
        self.relay = relay
        self.environment = environment
        self.topic = topic
        self.keys = keys
        self.requestAuthorization = requestAuthorization
    }

    /// The relay and APNs environment of this build, from `Info.plist` (set by `Config/Push.xcconfig`);
    /// `nil` when the build has no relay.
    convenience init?(state: AppState, bundle: Bundle = .main) {
        guard let relayURL = Self.relayURL(fromInfoValue: bundle.object(forInfoDictionaryKey: "WristcallRelayURL") as? String)
        else { return nil }
        self.init(
            state: state,
            relayURL: relayURL,
            relay: PushRelayClient(relayURL: relayURL),
            environment: (bundle.object(forInfoDictionaryKey: "WristcallPushEnvironment") as? String)
                .flatMap(PushEnvironment.init(rawValue:)) ?? .production,
            topic: bundle.bundleIdentifier ?? "")
    }

    /// Empty or unexpanded in the default builds: push off.
    nonisolated static func relayURL(fromInfoValue value: String?) -> URL? {
        guard let value, !value.isEmpty, let url = URL(string: value), url.host() != nil else { return nil }
        return url
    }

    /// Hangs on the app's hooks, keeping the ones already there. The hooks only queue the work: adding or
    /// removing a server does not wait for the relay.
    func install() {
        var hooks = state.hooks
        let added = hooks.serverAdded
        let changed = hooks.serverChanged
        let removed = hooks.serverRemoved
        hooks.serverAdded = { [weak self] server in
            await added?(server)
            self?.enqueue { await $0.added(server) }
        }
        hooks.serverChanged = { [weak self] server in
            await changed?(server)
            self?.enqueue { await $0.changed(server) }
        }
        hooks.serverRemoved = { [weak self] server in
            await removed?(server)
            self?.enqueue { await $0.removed(server) }
        }
        state.hooks = hooks
    }

    // MARK: - Registration

    /// At launch: asks APNs for a device token (the push build only). In a Debug build,
    /// `-WCFakeAPNsToken <hex>` stands in for it.
    func start(arguments: [String] = ProcessInfo.processInfo.arguments) {
        #if DEBUG
        if let index = arguments.firstIndex(of: "-WCFakeAPNsToken"), arguments.indices.contains(index + 1),
           let token = Data(hexString: arguments[index + 1]) {
            Task { await didRegister(deviceToken: token) }
            return
        }
        #endif
        #if WRISTCALL_PUSH
        UIApplication.shared.registerForRemoteNotifications()
        #endif
    }

    /// APNs answered with this launch's device token: registers (or checks) the key of every server.
    func didRegister(deviceToken: Data) async {
        self.deviceToken = deviceToken
        await sync()
    }

    /// Every activation checks the keys of every server again (R20).
    func sync() async {
        await enqueue { coordinator in
            guard coordinator.state.loadStoreIfNeeded() else { return }
            for server in coordinator.state.servers {
                await coordinator.sync(server)
            }
        }.value
    }

    func serverAdded(_ server: ManagedServer) async {
        await enqueue { await $0.added(server) }.value
    }

    /// Renamed, or linked to the account (which may make it the first one with an account).
    func serverChanged(_ server: ManagedServer) async {
        await enqueue { await $0.changed(server) }.value
    }

    /// The server is gone from the app: `DELETE /v1/push` with its token (best effort; the token still works
    /// there), then the key is dropped at the relay and here.
    func serverRemoved(_ server: ManagedServer) async {
        await enqueue { await $0.removed(server) }.value
    }

    /// The account was deleted (R18): every key of this iPhone is undone, at the servers and at the relay,
    /// and nothing is registered again until the next launch.
    func forgetAll() async {
        await enqueue { coordinator in
            coordinator.stopped = true
            _ = coordinator.state.loadStoreIfNeeded()
            for server in coordinator.state.servers {
                await coordinator.removed(server)
            }
        }.value
    }

    /// Waits for every queued step and the permission request (tests).
    func idle() async {
        await chain?.value
        await authorization?.value
    }

    // MARK: - Steps

    @discardableResult
    private func enqueue(_ work: @escaping @MainActor (PhonePushCoordinator) async -> Void) -> Task<Void, Never> {
        let previous = chain
        let task = Task { [weak self] in
            await previous?.value
            guard let self else { return }
            await work(self)
        }
        chain = task
        return task
    }

    private func added(_ server: ManagedServer) async {
        if state.healths[server.id]?.account != nil || server.linked { askForAuthorizationOnce() }
        await sync(server)
    }

    private func changed(_ server: ManagedServer) async {
        if server.linked || state.healths[server.id]?.account != nil { askForAuthorizationOnce() }
        await sync(server)
    }

    /// One server; a failure is logged (a status only, never a key or token) and the next one goes on.
    private func sync(_ server: ManagedServer) async {
        guard !stopped, let token = deviceToken else { return }
        let api = state.api(for: server)
        do {
            let health = try await api.health()
            // Approvals exist only on a server with an account: no key for one without.
            guard health.account != nil else {
                Self.log.notice("push: server has no account, skipped")
                return
            }
            guard let announced = health.relay else {
                Self.log.notice("push: server has push off, skipped")
                return
            }
            // R17: the app's relay only. A server naming another one would get the APNs token there.
            guard PushRelayClient.sameRelay(announced, relayURL) else {
                Self.log.notice("push: relay mismatch, server skipped")
                return
            }
            let tokenHex = token.hexString
            var key: String?
            if let stored = try? keys.load(serverID: server.id) {
                if stored.deviceToken != tokenHex {
                    // A new APNs token: the old key would push to a dead token.
                    try? await relay.unregister(pushKey: stored.pushKey)
                    try? keys.delete(serverID: server.id)
                } else if try await relay.isRegistered(pushKey: stored.pushKey) {
                    key = stored.pushKey
                } else {
                    try? keys.delete(serverID: server.id)
                }
            }
            if key == nil {
                let new = try await relay.register(
                    deviceToken: token, topic: topic, environment: environment,
                    label: Self.registrationLabel(for: server), tag: server.id, events: Self.events)
                // The server may have been removed (or the account deleted) while the relay answered.
                guard !stopped, state.servers.contains(where: { $0.id == server.id }) else {
                    try? await relay.unregister(pushKey: new)
                    return
                }
                do {
                    try keys.save(StoredPushKey(pushKey: new, deviceToken: tokenHex), serverID: server.id)
                } catch {
                    // Without it the next sync would register yet another key.
                    try? await relay.unregister(pushKey: new)
                    throw error
                }
                key = new
            }
            if let key {
                try await api.setPushKey(key)
                Self.log.notice("push: key handed to the server")
            }
        } catch PairingError.notFound {
            // `404 not_configured` from the relay or the server: no push there.
            Self.log.notice("push: not configured, server skipped")
        } catch {
            Self.log.error("push: sync failed: \(Self.describe(error), privacy: .public)")
        }
    }

    private func removed(_ server: ManagedServer) async {
        guard let stored = try? keys.load(serverID: server.id) else { return }
        do {
            try await state.api(for: server).clearPushKey()
        } catch {
            Self.log.error("push: clearing the key failed: \(Self.describe(error), privacy: .public)")
        }
        do {
            try await relay.unregister(pushKey: stored.pushKey)
        } catch {
            Self.log.error("push: unregister failed: \(Self.describe(error), privacy: .public)")
        }
        try? keys.delete(serverID: server.id)
    }

    // MARK: - Notification permission

    /// Asked the first time a server with an account is added or linked, when the user sees why; not at launch.
    private func askForAuthorizationOnce() {
        guard !askedForAuthorization else { return }
        askedForAuthorization = true
        let requestAuthorization = requestAuthorization
        authorization = Task { await requestAuthorization() }
    }

    static func requestNotificationAuthorization() async {
        let center = UNUserNotificationCenter.current()
        guard await center.notificationSettings().authorizationStatus == .notDetermined else { return }
        do {
            _ = try await center.requestAuthorization(options: [.alert, .sound])
        } catch {
            log.error("push: authorization request failed")
        }
    }

    // MARK: - Helpers

    /// The label the relay shows under every push of a registration: the server's name, cut to the relay's
    /// 64 characters (it refuses a longer one); its host when the name is empty.
    nonisolated static func registrationLabel(for server: ManagedServer) -> String {
        let name = server.name.trimmingCharacters(in: .whitespacesAndNewlines)
        let label = name.isEmpty ? (server.url.host() ?? "wristcall") : name
        return String(String.UnicodeScalarView(label.unicodeScalars.prefix(64)))
    }

    /// What may go to the log about a failure: a status or an error code, never a URL or a secret.
    private static func describe(_ error: any Error) -> String {
        switch error {
        case PairingError.unexpectedStatus(let status): "status \(status)"
        case PairingError.network(let code): "network \(code.rawValue)"
        case PairingError.unauthorized: "unauthorized"
        case PairingError.invalidRequest: "invalid request"
        case PairingError.rateLimited: "rate limited"
        case PairingError.malformedResponse: "malformed response"
        case APIError.network(let code): "network \(code.rawValue)"
        case APIError.unexpectedStatus(let status): "status \(status)"
        case is APIError: "server error"
        case let error as CredentialStoreError: "keychain \(error)"
        default: "other"
        }
    }
}

private extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }

    /// `nil` unless `hex` is an even number of hex digits.
    init?(hexString hex: String) {
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
