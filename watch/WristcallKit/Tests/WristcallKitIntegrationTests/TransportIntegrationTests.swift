#if os(macOS)
import Foundation
import Testing
import WristcallKit

/// `NWWebSocketTransport` against the local test server (`Tests/test-server.yaml`).
@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run integration tests"))
struct TransportIntegrationTests {
    @Test func validTokenGetsSessionReady() async throws {
        let device = try await TestDevices.shared()
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        try await transport.connect()
        try await transport.send(text: try ClientMessage.sessionStart(profile: "demo").jsonText())

        var events = transport.events.makeAsyncIterator()
        let first = try #require(await events.next())
        guard case .text(let text) = first, case .sessionReady(let ready) = try ServerMessage.decode(text) else {
            Issue.record("expected session.ready, got \(first)")
            return
        }
        #expect(ready.profile.name == "demo")
        #expect(ready.audioOut.codec == "pcm16")

        try await transport.send(text: try ClientMessage.sessionEnd.jsonText())
        #expect(await events.next() == .closed(code: CloseCode.normal.rawValue))
        #expect(await events.next() == nil)
        await transport.close(code: CloseCode.normal.rawValue)
    }

    @Test func invalidTokenIsClosedWith4401() async throws {
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: "not-a-real-token")
        try await transport.connect()
        var events = transport.events.makeAsyncIterator()
        #expect(await events.next() == .closed(code: CloseCode.unauthorized.rawValue))
        #expect(await events.next() == nil)
        await #expect(throws: TransportError.notConnected) {
            try await transport.send(text: "{}")
        }
    }

    @Test func revokedTokenIsClosedWith4401() async throws {
        let device = try await TestDevices.pair(name: "Integration Watch (revoked)")
        try TestDevices.revoke(device)
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        try await transport.connect()
        var events = transport.events.makeAsyncIterator()
        #expect(await events.next() == .closed(code: CloseCode.unauthorized.rawValue))
    }

    @Test func unreachableServerFailsToConnect() async throws {
        // Port 9 (discard) is closed on the test machines: connection refused.
        let transport = try NWWebSocketTransport(server: URL(string: "http://127.0.0.1:9")!, token: "x")
        await #expect(throws: TransportError.self) {
            try await transport.connect()
        }
        var events = transport.events.makeAsyncIterator()
        #expect(await events.next() == .closed(code: nil))
    }

    /// The server pings every 20 s and drops the connection 20 s after an unanswered ping.
    /// Idle for 45 s: the call survives only if autoReplyPing answers.
    @Test(.timeLimit(.minutes(1)))
    func idleCallSurvivesServerPings() async throws {
        let device = try await TestDevices.shared()
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        try await transport.connect()
        try await transport.send(text: try ClientMessage.sessionStart(profile: nil).jsonText())
        var events = transport.events.makeAsyncIterator()
        let first = try #require(await events.next())
        guard case .text(let text) = first, case .sessionReady = try ServerMessage.decode(text) else {
            Issue.record("expected session.ready, got \(first)")
            return
        }

        try await Task.sleep(for: .seconds(45))

        try await transport.send(text: try ClientMessage.sessionEnd.jsonText())
        #expect(await events.next() == .closed(code: CloseCode.normal.rawValue))
    }
}
#endif
