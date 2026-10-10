import Foundation
import Synchronization
import Testing
import WristcallKit
@testable import WristcallPhone

/// Push registration of the iPhone (push build only): one key per server with an account, at the app's
/// own relay, for `device.approval` alone, handed to the server with the personal token.
@MainActor
struct PhonePushCoordinatorTests {
    let relayURL = URL(string: "http://127.0.0.1:8090")!
    let token = Data([0x0a, 0x0b, 0x0c])
    let home = ManagedServer(id: "s1", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_h")
    let lab = ManagedServer(id: "s2", name: "Lab", url: URL(string: "https://lab.test")!, token: "wc_pat_l")
    let account = ServerHealth.AccountInfo(issuer: "http://127.0.0.1:8090", deviceCredential: "approval")
    let relay = FakePhoneRelay()
    let keys = MemoryPushKeyStore()
    let asked = Tally()

    struct World {
        let state: AppState
        let push: PhonePushCoordinator
        let fakes: [String: FakeServerAPI]
    }

    /// Servers answering the given health (default: this build's relay and an account).
    func world(_ servers: [(ManagedServer, ServerHealth?)]) -> World {
        var fakes: [String: FakeServerAPI] = [:]
        for (server, health) in servers {
            let fake = FakeServerAPI()
            fake.healthResult = .success(health ?? ServerHealth(version: "0.6.0", relay: relayURL, account: account))
            fakes[server.url.host()!] = fake
        }
        let byHost = fakes
        let state = AppState(store: InMemoryManagedServerStore(servers.map(\.0)), makeAPI: { url, _ in byHost[url.host()!]! })
        let asked = asked
        let push = PhonePushCoordinator(
            state: state, relayURL: relayURL, relay: relay, environment: .sandbox,
            topic: "io.github.ggondim.wristcall", keys: keys,
            requestAuthorization: { asked.count += 1 }
        )
        return World(state: state, push: push, fakes: fakes)
    }

    @Test func coordinatorRegistersDeviceApprovalOnly() async throws {
        let world = world([(home, nil), (lab, nil)])
        await world.push.didRegister(deviceToken: token)

        let registrations = relay.registrations
        #expect(registrations.count == 2)
        let first = try #require(registrations.first { $0.tag == "s1" })
        #expect(first.events == ["device.approval"])
        #expect(first.label == "Home")
        #expect(first.topic == "io.github.ggondim.wristcall")
        #expect(first.environment == .sandbox)
        #expect(first.deviceToken == token)
        #expect(registrations.first { $0.tag == "s2" }?.label == "Lab")
        // Each server got its own key, with its own personal token (the fake per host).
        #expect(world.fakes["home.test"]?.pushKeys == [first.key])
        #expect(world.fakes["lab.test"]?.pushKeys.count == 1)
        #expect(world.fakes["home.test"]?.pushKeys != world.fakes["lab.test"]?.pushKeys)
        #expect(keys.all["s1"] == StoredPushKey(pushKey: first.key, deviceToken: "0a0b0c"))
    }

    @Test func coordinatorSkipsForeignRelay() async {
        let other = ManagedServer(id: "s3", name: "Old", url: URL(string: "https://old.test")!, token: "wc_pat_o")
        let world = world([
            (home, ServerHealth(version: "0.6.0", relay: URL(string: "https://evil.test")!, account: account)),
            (lab, ServerHealth(version: "0.6.0", relay: nil, account: account)),
            // No account: the server never asks for an approval, so it gets no key either.
            (other, ServerHealth(version: "0.6.0", relay: relayURL, account: nil)),
        ])
        await world.push.didRegister(deviceToken: token)
        #expect(relay.registrations.isEmpty)
        #expect(keys.all.isEmpty)
        for fake in world.fakes.values {
            #expect(fake.calls == ["health"])
        }
    }

    @Test func sameRelayDespiteSpelling() async {
        let world = world([(home, ServerHealth(version: "0.6.0", relay: URL(string: "HTTP://127.0.0.1:8090/")!, account: account))])
        await world.push.didRegister(deviceToken: token)
        #expect(relay.registrations.count == 1)
    }

    @Test func nothingBeforeTheDeviceToken() async {
        let world = world([(home, nil)])
        await world.push.sync()
        await world.push.serverAdded(home)
        #expect(relay.registrations.isEmpty)
        #expect(world.fakes["home.test"]?.calls.contains("setPushKey") == false)
    }

    @Test func coordinatorReRegistersWhenGone() async throws {
        try keys.save(StoredPushKey(pushKey: "wc_push_old", deviceToken: "0a0b0c"), serverID: "s1")
        relay.forget("wc_push_old")
        let world = world([(home, nil)])
        await world.push.didRegister(deviceToken: token)
        #expect(relay.checked == ["wc_push_old"])
        let new = try #require(relay.registrations.first?.key)
        #expect(new != "wc_push_old")
        #expect(world.fakes["home.test"]?.pushKeys == [new])
        #expect(keys.all["s1"]?.pushKey == new)
    }

    @Test func aLiveKeyIsHandedAgainWithoutRegistering() async throws {
        try keys.save(StoredPushKey(pushKey: "wc_push_live", deviceToken: "0a0b0c"), serverID: "s1")
        relay.adopt("wc_push_live")
        let world = world([(home, nil)])
        await world.push.didRegister(deviceToken: token)
        await world.push.sync()
        #expect(relay.registrations.isEmpty)
        // `PUT /v1/push` is idempotent: every sync hands the key again (the server may have lost it).
        #expect(world.fakes["home.test"]?.pushKeys == ["wc_push_live", "wc_push_live"])
    }

    @Test func aNewDeviceTokenUnregistersTheOldKey() async throws {
        try keys.save(StoredPushKey(pushKey: "wc_push_old", deviceToken: "ffff"), serverID: "s1")
        relay.adopt("wc_push_old")
        let world = world([(home, nil)])
        await world.push.didRegister(deviceToken: token)
        #expect(relay.unregistered == ["wc_push_old"])
        let new = try #require(relay.registrations.first?.key)
        #expect(keys.all["s1"] == StoredPushKey(pushKey: new, deviceToken: "0a0b0c"))
    }

    @Test func removingServerClearsKey() async throws {
        let world = world([(home, nil), (lab, nil)])
        await world.push.didRegister(deviceToken: token)
        let key = try #require(keys.all["s1"]?.pushKey)
        await world.state.remove("s1")
        await world.push.serverRemoved(home)
        #expect(world.fakes["home.test"]?.calls.last == "clearPushKey")
        #expect(relay.unregistered == [key])
        #expect(keys.all["s1"] == nil)
        #expect(keys.all["s2"] != nil)
    }

    @Test func removingStillForgetsWhenTheServerFails() async throws {
        try keys.save(StoredPushKey(pushKey: "wc_push_k", deviceToken: "0a0b0c"), serverID: "s1")
        let world = world([(home, nil)])
        world.fakes["home.test"]?.pushKeyError = APIError.network(.cannotConnectToHost)
        await world.push.serverRemoved(home)
        #expect(relay.unregistered == ["wc_push_k"])
        #expect(keys.all.isEmpty)
    }

    @Test func aServerRemovedWhileRegisteringKeepsNoKey() async throws {
        let world = world([(home, nil)])
        let state = world.state
        relay.beforeReply = { await state.remove("s1") }
        await world.push.didRegister(deviceToken: token)
        let key = try #require(relay.registrations.first?.key)
        #expect(relay.unregistered == [key])
        #expect(keys.all.isEmpty)
        #expect(world.fakes["home.test"]?.calls.contains("setPushKey") == false)
    }

    @Test func addingAServerRegistersIt() async throws {
        let fake = FakeServerAPI()
        fake.healthResult = .success(ServerHealth(version: "0.6.0", relay: relayURL, account: account))
        let state = AppState(store: InMemoryManagedServerStore(), makeAPI: { _, _ in fake })
        let asked = asked
        let push = PhonePushCoordinator(
            state: state, relayURL: relayURL, relay: relay, environment: .sandbox,
            topic: "io.github.ggondim.wristcall", keys: keys, requestAuthorization: { asked.count += 1 }
        )
        push.install()
        await push.didRegister(deviceToken: token)
        #expect(relay.registrations.isEmpty)
        let saved = try await state.addServer(urlText: "https://home.test", token: "wc_pat_h", name: "Home")
        await push.idle()
        #expect(relay.registrations.map(\.tag) == [saved.id])
        #expect(relay.registrations.first?.label == "Home")
        #expect(fake.pushKeys.count == 1)
        // The first server with an account asks for notifications.
        #expect(asked.count == 1)
    }

    @Test func permissionAskedOnceForAServerWithAnAccount() async {
        let world = world([(home, nil), (lab, ServerHealth(version: "0.6.0", relay: relayURL, account: nil))])
        await world.state.load()
        #expect(asked.count == 0)
        await world.push.serverAdded(lab)
        await world.push.idle()
        #expect(asked.count == 0)
        await world.push.serverAdded(home)
        await world.push.idle()
        #expect(asked.count == 1)
        await world.push.serverAdded(home)
        var linked = home
        linked.linked = true
        await world.push.serverChanged(linked)
        await world.push.idle()
        #expect(asked.count == 1)
    }

    @Test func linkingAsksForPermission() async {
        let world = world([(lab, ServerHealth(version: "0.6.0", relay: relayURL, account: nil))])
        await world.state.load()
        var linked = lab
        linked.linked = true
        await world.push.serverChanged(linked)
        await world.push.idle()
        #expect(asked.count == 1)
    }

    @Test func hooksAreChainedAndKeepTheOthers() async throws {
        let world = world([(home, nil)])
        let seen = Tally()
        world.state.hooks.serverRemoved = { server in seen.log.append("removed \(server.id)") }
        world.push.install()
        await world.push.didRegister(deviceToken: token)
        #expect(keys.all["s1"] != nil)
        await world.state.remove("s1")
        await world.push.idle()
        #expect(seen.log == ["removed s1"])
        #expect(keys.all["s1"] == nil)
    }

    @Test func deletingTheAccountUndoesEveryKey() async throws {
        let world = world([(home, nil), (lab, nil)])
        await world.push.didRegister(deviceToken: token)
        #expect(keys.all.count == 2)
        let issued = Set(relay.registrations.map(\.key))
        await world.push.forgetAll()
        #expect(Set(relay.unregistered) == issued)
        #expect(keys.all.isEmpty)
        #expect(world.fakes["home.test"]?.pushKeys.isEmpty == true)
        #expect(world.fakes["lab.test"]?.pushKeys.isEmpty == true)
        // Not registered again in this launch.
        await world.push.sync()
        #expect(relay.registrations.count == 2)
    }

    @Test func registrationLabelIsTheNameUpTo64Characters() {
        #expect(PhonePushCoordinator.registrationLabel(for: home) == "Home")
        var long = home
        long.name = String(repeating: "é", count: 70)
        #expect(PhonePushCoordinator.registrationLabel(for: long).unicodeScalars.count == 64)
        var empty = home
        empty.name = ""
        #expect(PhonePushCoordinator.registrationLabel(for: empty) == "home.test")
    }

    @Test func relayURLFromInfo() {
        #expect(PhonePushCoordinator.relayURL(fromInfoValue: "http://127.0.0.1:8090") == URL(string: "http://127.0.0.1:8090"))
        #expect(PhonePushCoordinator.relayURL(fromInfoValue: "") == nil)
        #expect(PhonePushCoordinator.relayURL(fromInfoValue: nil) == nil)
        #expect(PhonePushCoordinator.relayURL(fromInfoValue: "$(WRISTCALL_RELAY_URL)") == nil)
    }
}

/// The relay: hands out keys, remembers which are registered, records what it got.
final class FakePhoneRelay: PhonePushRelaying, @unchecked Sendable {
    struct Registration {
        let deviceToken: Data
        let topic: String
        let environment: PushEnvironment
        let label: String
        let tag: String
        let events: [String]
        let key: String
    }

    private struct Book {
        var registrations: [Registration] = []
        var live: Set<String> = []
        var checked: [String] = []
        var unregistered: [String] = []
        var next = 0
    }

    private let book = Mutex(Book())
    /// Runs before `register` answers (something that happens while the relay is asked).
    var beforeReply: (@Sendable () async -> Void)?

    var registrations: [Registration] { book.withLock { $0.registrations } }
    var checked: [String] { book.withLock { $0.checked } }
    var unregistered: [String] { book.withLock { $0.unregistered } }

    func adopt(_ key: String) { _ = book.withLock { $0.live.insert(key) } }
    func forget(_ key: String) { _ = book.withLock { $0.live.remove(key) } }

    func register(
        deviceToken: Data, topic: String, environment: PushEnvironment, label: String, tag: String, events: [String]
    ) async throws -> String {
        let key = book.withLock { book in
            book.next += 1
            let key = "wc_push_\(book.next)"
            book.live.insert(key)
            book.registrations.append(Registration(
                deviceToken: deviceToken, topic: topic, environment: environment, label: label, tag: tag,
                events: events, key: key))
            return key
        }
        await beforeReply?()
        return key
    }

    func isRegistered(pushKey: String) async throws -> Bool {
        book.withLock { book in
            book.checked.append(pushKey)
            return book.live.contains(pushKey)
        }
    }

    func unregister(pushKey: String) async throws {
        book.withLock { book in
            book.unregistered.append(pushKey)
            book.live.remove(pushKey)
        }
    }
}

final class MemoryPushKeyStore: PushKeyStore {
    private let keys = Mutex<[String: StoredPushKey]>([:])
    var all: [String: StoredPushKey] { keys.withLock { $0 } }
    func load(serverID: String) throws -> StoredPushKey? { keys.withLock { $0[serverID] } }
    func save(_ key: StoredPushKey, serverID: String) throws { keys.withLock { $0[serverID] = key } }
    func delete(serverID: String) throws { keys.withLock { _ = $0.removeValue(forKey: serverID) } }
}

@MainActor
final class Tally {
    var count = 0
    var log: [String] = []
}
