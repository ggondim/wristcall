import Foundation
import Testing

@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run the integration tests"))
struct ServerSmokeTests {
    @Test func healthReportsProtocolVersion1() async throws {
        let url = try #require(TestServer.baseURL).appending(path: "v1/health")
        let (data, response) = try await URLSession.shared.data(from: url)
        #expect((response as? HTTPURLResponse)?.statusCode == 200)
        let body = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(body["status"] as? String == "ok")
        #expect(body["protocol"] as? Int == 1)
    }

    #if os(macOS)
    @Test func cliIssuesPairingCodes() throws {
        let first = try TestServer.newPairingCode()
        let second = try TestServer.newPairingCode()
        #expect(first.count == 8)
        #expect(first != second)
    }
    #endif
}
