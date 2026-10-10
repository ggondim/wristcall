import Foundation
import Testing
import WristcallKit
@testable import Wristcall

/// What the watch does with a message from the iPhone (decision R10), without WatchConnectivity.
@MainActor
struct WatchLinkReceiverTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let defaults = UserDefaults(suiteName: "WatchLinkReceiverTests.\(UUID().uuidString)")!
    let first = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token", id: "srv-1")
    let device = PairedDevice(deviceId: "dev-5", token: "new-token")
    let info = DeviceInfo(
        deviceId: "dev-1", deviceName: "Apple Watch", user: UserInfo(id: "usr_1", handle: "gustavo"),
        agents: [Agent(id: "ag_1", slug: "default", displayName: "Agent")])
    let homeInfo = DeviceInfo(
        deviceId: "dev-5", deviceName: "Apple Watch", user: UserInfo(id: "usr_9", handle: "family"),
        agents: [Agent(id: "ag_9", slug: "house", displayName: "House")])
    let home = URL(string: "https://home.example.com")!
    let code = PairingCode("12345678")!
    /// The code's expiry, 10 minutes from now.
    let soon = Date().timeIntervalSince1970 + 600

    private func makeModel() -> AppModel {
        AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in })
    }

    private func makePairedModel() async throws -> AppModel {
        try store.save([first])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        let model = makeModel()
        await model.launch()
        try #require(model.phase == .home)
        return model
    }

    @Test func pairMessageAddsServer() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        pairing.pairResults = [.success(.paired(device))]
        pairing.setMeResults([.success(homeInfo)], for: home)

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply == WatchLinkReply(ok: true))
        #expect(pairing.calls.contains("pair https://home.example.com code=12345678 name=Apple Watch"))
        #expect(model.servers.map(\.credentials.serverURL) == [first.serverURL, home])
        #expect(try store.load().map(\.token) == ["device-token", "new-token"])
        #expect(model.phase == .home)
        // Flow A': the code goes to the server the iPhone named, never through the directory.
        #expect(!pairing.calls.contains { $0.hasPrefix("resolve") })
    }

    @Test func pairMessageOnPairingScreenAddsFirstServer() async {
        let model = makeModel()
        await model.launch()
        let receiver = WatchLinkReceiver(model: model)
        pairing.pairResults = [.success(.paired(device))]
        pairing.setMeResults([.success(homeInfo)], for: home)

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply.ok)
        #expect(model.phase == .home)
        #expect(model.servers.count == 1)
    }

    @Test func pairMessageKeepsTypedServerURLOnFailure() async throws {
        let model = makeModel()
        await model.launch()
        model.useServerURL("https://typed.example.com")
        let receiver = WatchLinkReceiver(model: model)
        pairing.pairResults = [.failure(.invalidCode)]

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply == WatchLinkReply(ok: false, error: AppModel.Message.invalidCode))
        #expect(model.phase == .unpaired)
        #expect(model.customServerURL == URL(string: "https://typed.example.com"))
    }

    @Test func pairFailureFromHomeStaysHome() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        pairing.pairResults = [.failure(.invalidCode)]

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply == WatchLinkReply(ok: false, error: AppModel.Message.invalidCode))
        #expect(model.phase == .home)
        #expect(model.servers.map(\.credentials) == [first])
    }

    /// I1: a transfer queued on the iPhone is delivered right after activation, before the servers
    /// were read: it waits for launch (starting it), then pairs.
    @Test func queuedPairBeforeLaunchIsApplied() async throws {
        try store.save([first])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.pairResults = [.success(.paired(device))]
        pairing.setMeResults([.success(homeInfo)], for: home)
        let model = makeModel()
        let receiver = WatchLinkReceiver(model: model)
        #expect(model.phase == .launching)

        receiver.receive(.pair(server: home, code: code, name: "Home", expiresAt: soon), reply: nil)
        await waitUntil { model.servers.count == 2 && !model.isBusy }

        #expect(model.phase == .home)
        #expect(model.servers.map(\.credentials.serverURL) == [first.serverURL, home])
        #expect(pairing.calls.first == "me https://agent.example.com")
        #expect(receiver.lastContext == WatchLinkContext(servers: ["https://agent.example.com", "https://home.example.com"]))
        // The scene's own launch afterwards does not read the Keychain again.
        await model.launchIfNeeded()
        #expect(pairing.calls.filter { $0 == "me https://agent.example.com" }.count == 1)
    }

    @Test func messagesBeforeLaunchKeepTheirOrderAndReplies() async throws {
        let model = makeModel()
        let receiver = WatchLinkReceiver(model: model)
        let replies = Recorder<WatchLinkReply>()
        receiver.receive(nil) { replies.append($0) }
        receiver.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: soon)) { replies.append($0) }
        await waitUntil { replies.all.count == 2 }
        #expect(replies.all == [
            WatchLinkReply(ok: false, error: WatchLinkReply.Reason.invalid),
            WatchLinkReply(ok: false, error: WatchLinkReply.Reason.unsupported),
        ])
        #expect(model.phase == .unpaired)
    }

    /// I2: a queued transfer delivered after its code expired does nothing and says nothing.
    @Test func expiredPairIsIgnored() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: Date().timeIntervalSince1970 - 1))

        #expect(reply == WatchLinkReply(ok: false, error: WatchLinkReply.Reason.expired))
        #expect(!pairing.calls.contains { $0.hasPrefix("pair") })
        #expect(model.phase == .home)
        #expect(model.message == nil)
    }

    /// I3: a server in manual mode answers 202: the watch tells the iPhone at once (so the owner can
    /// approve on it) and keeps waiting for the approval.
    @Test func pendingRepliesAtOnceAndKeepsWaiting() async throws {
        let gate = Gate()
        try store.save([first])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in await gate.wait() })
        await model.launch()
        let receiver = WatchLinkReceiver(model: model)
        let request = PairingRequest(requestId: "4821", pollToken: "poll-secret", expiresAt: .now.addingTimeInterval(600))
        pairing.pairResults = [.success(.pending(request))]
        pairing.pollResults = [.success(.paired(device))]
        pairing.setMeResults([.success(homeInfo)], for: home)
        let replies = Recorder<WatchLinkReply>()

        let running = Task { await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon)) { replies.append($0) } }
        await waitUntil { !replies.all.isEmpty }

        #expect(replies.all == [WatchLinkReply(ok: true, pending: true, requestId: "4821")])
        #expect(model.phase == .pairing(requestId: "4821"))
        // The poll token never leaves the watch.
        #expect(replies.all.first?.dictionary.values.contains { ($0 as? String) == "poll-secret" } == false)

        gate.open()
        await running.value
        #expect(replies.all.count == 1)
        #expect(model.servers.map(\.credentials.serverURL) == [first.serverURL, home])
        #expect(receiver.lastContext?.servers.contains("https://home.example.com") == true)
    }

    @Test func busyDuringCall() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        let target = try #require(model.agents.first)
        model.startCall(target)
        try #require(model.phase == .inCall(target))

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply == WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy))
        #expect(!pairing.calls.contains { $0.hasPrefix("pair") })
        #expect(model.phase == .inCall(target))
    }

    @Test func busyWhilePairing() async throws {
        let gate = Gate()
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in await gate.wait() })
        await model.launch()
        let receiver = WatchLinkReceiver(model: model)
        model.useServerURL("https://typed.example.com")
        let request = PairingRequest(requestId: "4821", pollToken: "poll-secret", expiresAt: .now.addingTimeInterval(600))
        pairing.pairResults = [.success(.pending(request))]
        let typed = model.requestApproval()
        await waitUntil { model.phase == .pairing(requestId: "4821") }

        let reply = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))

        #expect(reply == WatchLinkReply(ok: false, error: WatchLinkReply.Reason.busy))
        #expect(pairing.calls.filter { $0.hasPrefix("pair") }.count == 1)
        model.cancelPairing()
        gate.open()
        await typed.value
    }

    @Test func refreshReloadsAgents() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        let renamed = DeviceInfo(
            deviceId: "dev-1", deviceName: "Apple Watch", user: UserInfo(id: "usr_1", handle: "gustavo"),
            agents: [Agent(id: "ag_1", slug: "default", displayName: "Renamed")])
        pairing.setMeResults([.success(renamed)], for: first.serverURL)

        let reply = await receiver.handle(.refresh)

        #expect(reply == WatchLinkReply(ok: true))
        #expect(pairing.calls.filter { $0 == "me https://agent.example.com" }.count == 2)
        #expect(model.agents.map(\.agent.displayName) == ["Renamed"])
    }

    @Test func deviceCodeIsNotForTheWatch() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        let reply = await receiver.handle(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000))
        #expect(reply == WatchLinkReply(ok: false, error: WatchLinkReply.Reason.unsupported))
    }

    @Test func contextListsCanonicalURLs() async throws {
        let odd = Credentials(
            serverURL: URL(string: "https://Home.Example.com:443/wc")!, deviceId: "dev-9", token: "home-token", id: "srv-2")
        try store.save([first, odd])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: odd.serverURL)
        let model = makeModel()
        let receiver = WatchLinkReceiver(model: model)

        await model.launch()

        #expect(receiver.lastContext == WatchLinkContext(servers: ["https://agent.example.com", "https://home.example.com/wc"]))
        // Only addresses: no token, no device id.
        let published = try #require(receiver.lastContext?.dictionary)
        #expect(Set(published.keys) == ["v", "servers"])
    }

    @Test func contextFollowsPairingAndRemoval() async throws {
        let model = try await makePairedModel()
        let receiver = WatchLinkReceiver(model: model)
        pairing.pairResults = [.success(.paired(device))]
        pairing.setMeResults([.success(homeInfo)], for: home)

        _ = await receiver.handle(.pair(server: home, code: code, name: "Home", expiresAt: soon))
        #expect(receiver.lastContext == WatchLinkContext(servers: ["https://agent.example.com", "https://home.example.com"]))

        await model.removeServer(id: "srv-1")
        #expect(receiver.lastContext == WatchLinkContext(servers: ["https://home.example.com"]))
    }

    @Test func emptyWatchPublishesEmptyContext() async {
        let model = makeModel()
        let receiver = WatchLinkReceiver(model: model)
        await model.launch()
        #expect(receiver.lastContext == WatchLinkContext(servers: []))
    }
}
