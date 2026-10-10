#if os(macOS)
import Foundation
import Testing
import WristcallKit

/// The management client against the local test server, with a personal token from
/// `wristcall users tokens add`. The agent it creates starts with `it-` (see `OneShotAgent`), so the other
/// suites, which run in parallel and list the user's agents, leave it out.
@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run integration tests"))
struct ManagementIntegrationTests {
    private static let slug = "it-kit-e7"

    /// A new personal token: the CLI prints it once, on the line that starts with `wc_pat_`.
    private func personalToken(name: String) throws -> String {
        let output = try TestServer.runCLI(["users", "tokens", "add", "--name", name])
        let line = output.split(whereSeparator: \.isNewline).first { $0.hasPrefix(ManagementClient.personalTokenPrefix) }
        return String(try #require(line)).trimmingCharacters(in: .whitespaces)
    }

    private func client(name: String = "kit-test") throws -> ManagementClient {
        ManagementClient(server: try TestServer.requireBaseURL(), token: try personalToken(name: name))
    }

    @Test func verifyAndProviders() async throws {
        let client = try client()
        try await client.verify()
        let list = try await client.providers()
        #expect(Set(list.providers.map(\.name)).isSuperset(of: ["demo-stt", "demo-chat", "demo-tts"]))
        #expect(list.providers.first { $0.name == "demo-stt" }?.kind == "stt")
    }

    @Test func verifyRejectsADeviceToken() async throws {
        let device = try await TestDevices.shared()
        let client = ManagementClient(server: try TestServer.requireBaseURL(), token: device.token)
        do {
            try await client.verify()
            Issue.record("a device token must not pass verify()")
        } catch let error as APIError {
            guard case .forbidden = error else {
                Issue.record("expected .forbidden, got \(error)")
                return
            }
        }
    }

    @Test func agentLifecycle() async throws {
        let client = try client()
        // Left over by a run killed before cleanup.
        try? await client.deleteAgent(Self.slug)

        let created = try await client.createAgent([
            "slug": .string(Self.slug),
            "display_name": .string("Kit test"),
            "call_type": .string("one-shot"),
            "stt": .object(["provider": .string("demo-stt")]),
            "action": .object(["type": .string("webhook"), "url": .string("https://example.com/hook")]),
        ])
        #expect(created.slug == Self.slug)
        #expect(created.isOneWay)
        #expect(created.displayName == "Kit test")
        #expect(created.tts == nil)

        let read = try await client.agent(Self.slug)
        #expect(read == created)
        #expect(try await client.agents().contains { $0.id == created.id })

        let renamed = try await client.updateAgent(Self.slug, ["display_name": .string("Kit test 2"), "retention_days": .int(7)])
        #expect(renamed.displayName == "Kit test 2")
        #expect(renamed.retentionDays == .int(7))

        await #expect(throws: APIError.self) { try await client.createAgent(["slug": .string(Self.slug)]) }

        try await client.deleteAgent(Self.slug)
        await #expect(throws: APIError.notFound) { try await client.agent(Self.slug) }
    }

    @Test func invalidAgentIsRefusedWithAMessage() async throws {
        let client = try client()
        do {
            _ = try await client.createAgent(["slug": .string("Not A Slug")])
            Issue.record("expected the server to refuse the slug")
        } catch let error as APIError {
            guard case .invalid(let message) = error else {
                Issue.record("expected .invalid, got \(error)")
                return
            }
            #expect(message.contains("slug"))
        }
    }

    @Test func devicesAndPairingCode() async throws {
        let client = try client()
        let devices = try await client.devices()
        #expect(devices.allSatisfy { !$0.id.isEmpty })

        let grant = try await client.createPairingCode()
        #expect(PairingCode(grant.code) != nil)
        #expect(grant.code.count == 8)
        #expect(grant.expiresAt > Date().timeIntervalSince1970)
        #expect(grant.serverUrl == (try TestServer.requireBaseURL()).absoluteString)
    }

    @Test func accountRoutesNeedAnAccountOnTheServer() async throws {
        let client = try client()
        // The test server has no central account.
        await #expect(throws: APIError.notConfigured) { try await client.pairingRequests() }
        await #expect(throws: APIError.notConfigured) { try await client.unlinkAccount() }
        let health = try await ServerPushClient.health(of: try TestServer.requireBaseURL())
        #expect(health.account == nil)
    }

    @Test func aTokenOfAnotherServerIsUnauthorized() async throws {
        let client = ManagementClient(server: try TestServer.requireBaseURL(), token: "wc_pat_not-a-real-token")
        await #expect(throws: APIError.unauthorized) { try await client.verify() }
    }
}
#endif
