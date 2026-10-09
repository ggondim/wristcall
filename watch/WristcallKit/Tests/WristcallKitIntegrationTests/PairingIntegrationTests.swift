import Foundation
import Testing
import WristcallKit

/// Pairing against a real server started with `Tests/test-server.yaml` (`pairing_approval: code`).
/// Flow B needs `pairing_approval: manual`, which that config does not use; `PairingClientTests` covers it with stubs.
/// The server allows 10 `POST /v1/pair` per minute per IP: keep the number of pairings here small.
@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run the integration tests"))
struct PairingIntegrationTests {
    let client = PairingClient()

    #if os(macOS)
    @Test func pairMeUnpairWithAFreshCode() async throws {
        let server = try #require(TestServer.baseURL)
        let code = try #require(PairingCode(try TestServer.newPairingCode()))

        let result = try await client.pair(server: server, code: code, deviceName: "Integration Watch")
        guard case .paired(let device) = result else {
            Issue.record("expected .paired, got \(result)")
            return
        }
        #expect(!device.token.isEmpty)

        let info = try await client.me(server: server, token: device.token)
        #expect(info.deviceId == device.deviceId)
        #expect(info.deviceName == "Integration Watch")
        // The config's two profiles; agents that `OneWayCallIntegrationTests` add meanwhile do not count.
        let configured = info.agents.filter { !OneShotAgent.isTestAgent($0.slug) }
        #expect(info.profiles.map(\.name).filter { !OneShotAgent.isTestAgent($0) }.sorted() == ["default", "demo"])
        #expect(configured.map(\.slug).sorted() == ["default", "demo"])
        #expect(configured.allSatisfy { $0.callType == .conversation && !$0.id.isEmpty })

        // The code is single use.
        await #expect(throws: PairingError.invalidCode) {
            try await client.pair(server: server, code: code, deviceName: "Second Watch")
        }

        try await client.unpair(server: server, token: device.token)
        await #expect(throws: PairingError.unauthorized) {
            try await client.me(server: server, token: device.token)
        }
    }
    #endif

    @Test func pairWithoutACodeIsRefusedInCodeMode() async throws {
        let server = try #require(TestServer.baseURL)
        await #expect(throws: PairingError.invalidCode) {
            try await client.pair(server: server, code: nil, deviceName: "Integration Watch")
        }
    }

    @Test func pollWithAnUnknownTokenIsGone() async throws {
        let server = try #require(TestServer.baseURL)
        #expect(try await client.poll(server: server, pollToken: "not-a-real-token") == .gone)
    }

    @Test func meWithAnUnknownTokenIsUnauthorized() async throws {
        let server = try #require(TestServer.baseURL)
        await #expect(throws: PairingError.unauthorized) {
            try await client.me(server: server, token: "not-a-real-token")
        }
    }
}
