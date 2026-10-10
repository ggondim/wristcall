import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

struct LiveServerAPITests {
    @Test func pushKeyIsForwardedWithThePersonalToken() async throws {
        let host = StubHost { _ in .init(204, "") }
        let api = LiveServerAPI(server: host.url, token: "wc_pat_tok", session: .stubbed())
        try await api.setPushKey("k-1")
        try await api.clearPushKey()
        let requests = host.requests
        #expect(requests.map(\.method) == ["PUT", "DELETE"])
        #expect(requests.allSatisfy { $0.path == "/v1/push" })
        #expect(requests.allSatisfy { $0.headers.contains { $0.key.lowercased() == "authorization" && $0.value == "Bearer wc_pat_tok" } })
        #expect(try requests[0].json()["push_key"] as? String == "k-1")
    }

    @Test func healthUsesTheInjectedSession() async throws {
        let host = StubHost { _ in .init(200, #"{"status":"ok","version":"0.6.0","protocol":1,"account":null,"push":null}"#) }
        let api = LiveServerAPI(server: host.url, token: "wc_pat_tok", session: .stubbed())
        #expect(try await api.health().version == "0.6.0")
        #expect(host.requests.first?.path == "/v1/health")
    }
}
