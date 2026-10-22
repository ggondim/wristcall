import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct ClientIdentityTests {
    static let watch = ClientIdentity(platform: .watchos, version: "0.6.0")!

    @Test func clientHeaderMatchesEnrollPlatform() {
        #expect(ClientIdentity(platform: .ios, version: "0.3.0")?.header == "ios/0.3.0")
        #expect(ClientIdentity(platform: .watchos, version: "0.6.0")?.header == "watchos/0.6.0")
        #expect(ClientIdentity.headerName == "X-Wristcall-Client")
    }

    @Test func validVersionsAreKept() {
        #expect(ClientIdentity(platform: .web, version: "0.4.0")?.version == "0.4.0")
        #expect(ClientIdentity(platform: .web, version: "1234.5678.9012")?.header == "web/1234.5678.9012")
    }

    @Test(arguments: ["0.6.0-beta", "0.6", "", "1.2.3.4", "12345.0.0", "a.b.c", " 0.6.0", "0.6.0\n", "0.6.0 "])
    func invalidVersionIsRejected(version: String) {
        #expect(ClientIdentity(platform: .watchos, version: version) == nil)
    }

    @Test func fromBundleWithoutTheKeyIsNil() {
        // `swift test` has no `WristcallClientVersion` key in any bundle.
        #expect(ClientIdentity.fromBundle(Bundle(for: BundleToken.self), platform: .watchos) == nil)
        #expect(ClientIdentity.fromBundle(.main, platform: .ios) == nil)
    }

    @Test func cloudRequestsCarryClientHeader() async throws {
        let host = StubHost(replies: [
            (200, #"{"servers":[]}"#),
            (200, #"{"account":"acc_abc","created_at":1800000000,"servers":0,"plan":null,"identities":[],"key_set":true}"#),
            (200, CloudSessionRoutesTests.sessionJSON),
        ])
        let client = CloudClient(cloud: host.url, session: .stubbed(), client: Self.watch)
        _ = try await client.servers(accessToken: "at")
        _ = try await client.account(accessToken: "at")
        _ = try await client.refreshSession("wcr_old")
        #expect(host.requests.count == 3)
        #expect(host.requests.allSatisfy { $0.headers["X-Wristcall-Client"] == "watchos/0.6.0" })
    }

    @Test func relayRequestsCarryClientHeader() async throws {
        let relay = StubHost(replies: [
            (201, #"{"push_key":"wc_push_abc","created_at":1}"#),
            (200, "{}"),
            (204, ""),
        ])
        let client = PushRelayClient(relayURL: relay.url, session: .stubbed(), client: Self.watch)
        let key = try await client.register(
            deviceToken: Data([1]), topic: "t", environment: .sandbox, label: "l", tag: "x")
        _ = try await client.isRegistered(pushKey: key)
        try await client.unregister(pushKey: key)
        #expect(relay.requests.count == 3)
        #expect(relay.requests.allSatisfy { $0.headers["X-Wristcall-Client"] == "watchos/0.6.0" })
    }

    @Test func noClientMeansNoHeader() async throws {
        let host = StubHost(replies: [(200, #"{"servers":[]}"#)])
        _ = try await CloudClient(cloud: host.url, session: .stubbed()).servers(accessToken: "at")
        #expect(host.requests.allSatisfy { $0.headers["X-Wristcall-Client"] == nil })

        let relay = StubHost(replies: [(201, #"{"push_key":"k","created_at":1}"#)])
        _ = try await PushRelayClient(relayURL: relay.url, session: .stubbed()).register(
            deviceToken: Data([1]), topic: "t", environment: .sandbox, label: "l", tag: "x")
        #expect(relay.requests.allSatisfy { $0.headers["X-Wristcall-Client"] == nil })
    }

    @Test func serverPushClientNeverSendsTheHeader() async throws {
        let server = StubHost(replies: [(200, #"{"status":"ok","version":"0.6.0","protocol":1}"#)])
        _ = try await ServerPushClient.health(of: server.url, session: .stubbed())
        #expect(!server.requests.isEmpty)
        #expect(server.requests.allSatisfy { $0.headers["X-Wristcall-Client"] == nil })
    }

    @Test func sessionsPassTheClientToTheCloudClient() async {
        let device = DeviceSession(
            cloud: URL(string: "https://cloud.test")!, store: InMemoryDeviceSessionStore(), client: Self.watch)
        #expect(device.cloudClient.client == Self.watch)
        let account = AccountSession(
            cloud: URL(string: "https://cloud.test")!, kind: .watch, store: InMemoryTokenStore(),
            client: Self.watch)
        #expect(account.cloudClient.client == Self.watch)
    }
}

private final class BundleToken {}
