import Foundation
import Testing
import WristcallKit

struct PushRelayClientTests {
    let token = Data([0x00, 0x0A, 0xFF, 0x1B])

    func register(_ relay: StubHost) async throws -> String {
        try await PushRelayClient(relayURL: relay.url, session: .stubbed()).register(
            deviceToken: token,
            topic: "br.com.trigram.wristcall.watchkitapp",
            environment: .sandbox,
            label: "Ana's Apple Watch",
            tag: "server-1"
        )
    }

    @Test func registerSendsTheBodyAndReadsThePushKey() async throws {
        let relay = StubHost(replies: [(201, #"{"push_key":"wc_push_abc","created_at":1}"#)])
        let key = try await register(relay)
        #expect(key == "wc_push_abc")
        let request = try #require(relay.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/push/registrations")
        #expect(request.headers["Content-Type"] == "application/json")
        #expect(request.headers["Authorization"] == nil)
        let body = try request.json()
        #expect(body["platform"] as? String == "apns")
        #expect(body["token"] as? String == "000aff1b")
        #expect(body["topic"] as? String == "br.com.trigram.wristcall.watchkitapp")
        #expect(body["environment"] as? String == "sandbox")
        #expect(body["label"] as? String == "Ana's Apple Watch")
        #expect(body["tag"] as? String == "server-1")
        #expect(body["events"] as? [String] == ["call.finished"])
    }

    @Test func registerKeepsTheRelayPathPrefix() async throws {
        let relay = StubHost(path: "/relay", replies: [(201, #"{"push_key":"k"}"#)])
        _ = try await register(relay)
        #expect(relay.requests.first?.path == "/relay/v1/push/registrations")
    }

    @Test(arguments: [
        (422, #"{"detail":[]}"#, PairingError.invalidRequest),
        (429, #"{"error":"rate_limited"}"#, .rateLimited),
        (500, "oops", .unexpectedStatus(500)),
        (200, #"{"push_key":"k"}"#, .unexpectedStatus(200)),
        (201, #"{"key":"k"}"#, .malformedResponse),
    ])
    func registerErrors(status: Int, body: String, expected: PairingError) async throws {
        let relay = StubHost(replies: [(status, body)])
        await #expect(throws: expected) { try await register(relay) }
    }

    @Test func registerWrapsNetworkFailures() async throws {
        let relay = StubHost { _ in throw URLError(.notConnectedToInternet) }
        await #expect(throws: PairingError.network(.notConnectedToInternet)) { try await register(relay) }
    }

    @Test func isRegisteredIsTrueOn200AndFalseOn410() async throws {
        let present = StubHost(replies: [(200, #"{"label":"x"}"#)])
        let client = PushRelayClient(relayURL: present.url, session: .stubbed())
        #expect(try await client.isRegistered(pushKey: "wc_push_abc"))
        let request = try #require(present.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/push/registrations/current")
        #expect(request.headers["Authorization"] == "Bearer wc_push_abc")

        let gone = StubHost(replies: [(410, #"{"error":"gone"}"#)])
        let goneClient = PushRelayClient(relayURL: gone.url, session: .stubbed())
        #expect(try await goneClient.isRegistered(pushKey: "wc_push_abc") == false)
    }

    @Test(arguments: [(401, PairingError.unauthorized), (429, .rateLimited), (503, .unexpectedStatus(503))])
    func isRegisteredThrowsOnOtherStatuses(status: Int, expected: PairingError) async throws {
        let relay = StubHost(replies: [(status, "{}")])
        let client = PushRelayClient(relayURL: relay.url, session: .stubbed())
        await #expect(throws: expected) { try await client.isRegistered(pushKey: "k") }
    }

    @Test(arguments: [204, 404])
    func unregisterTreatsNoContentAndNotFoundAsDone(status: Int) async throws {
        let relay = StubHost(replies: [(status, "")])
        try await PushRelayClient(relayURL: relay.url, session: .stubbed()).unregister(pushKey: "wc_push_abc")
        let request = try #require(relay.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/push/registrations/current")
        #expect(request.headers["Authorization"] == "Bearer wc_push_abc")
    }

    @Test func unregisterThrowsOnServerErrors() async throws {
        let relay = StubHost(replies: [(500, "")])
        let client = PushRelayClient(relayURL: relay.url, session: .stubbed())
        await #expect(throws: PairingError.unexpectedStatus(500)) { try await client.unregister(pushKey: "k") }
    }
}
