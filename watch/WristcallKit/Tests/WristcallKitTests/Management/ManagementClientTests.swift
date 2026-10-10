import Foundation
import Testing
import WristcallKit

struct ManagementClientTests {
    private static let agentJSON = #"""
    {"id":"ag_1","slug":"notes","display_name":"Notes","icon":"waveform","call_type":"one-shot","turn_end":"auto",
     "position":2,"language":"en","stt":{"provider":"demo-stt"},"action":{"type":"webhook","url":"https://example.com/hook"},
     "tts":null,"system_prompt":"","fallback_message":"Sorry","vad":{"threshold":0.5},"timeouts":{"idle_s":30},
     "retention_days":null,"effective_retention_days":30,"created_at":1.5,"updated_at":2.5}
    """#

    private func client(_ host: StubHost, token: String = "wc_pat_x") -> ManagementClient {
        ManagementClient(server: host.url, token: token, session: .stubbed())
    }

    // MARK: - Verification and providers

    @Test func verifyRejectsDeviceToken() async throws {
        let host = StubHost(replies: [(403, #"{"error":"forbidden","message":"this needs user API token, not device token"}"#)])
        let client = ManagementClient(server: host.url, token: "abc", session: .stubbed())
        await #expect(throws: APIError.forbidden("this needs user API token, not device token")) { try await client.verify() }
        #expect(host.requests.first?.path == "/v1/providers")
    }

    @Test func verifyAcceptsAPersonalToken() async throws {
        let host = StubHost(replies: [(200, #"{"providers":[],"custom_endpoints":false}"#)])
        try await client(host).verify()
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.headers["Authorization"] == "Bearer wc_pat_x")
    }

    @Test func providersDecode() async throws {
        let host = StubHost(replies: [(200, #"{"providers":[{"name":"demo-stt","kind":"stt"},{"name":"demo-chat","kind":"action"}],"custom_endpoints":true}"#)])
        let list = try await client(host).providers()
        #expect(list.providers == [Provider(name: "demo-stt", kind: "stt"), Provider(name: "demo-chat", kind: "action")])
        #expect(list.customEndpoints)
    }

    @Test func serverPathPrefixIsKept() async throws {
        let host = StubHost(path: "/wristcall", replies: [(200, #"{"agents":[]}"#)])
        _ = try await client(host).agents()
        #expect(host.requests.first?.path == "/wristcall/v1/agents")
    }

    // MARK: - Agents

    @Test func agentsDecodeTheOwnerView() async throws {
        let host = StubHost(replies: [(200, #"{"agents":[\#(Self.agentJSON)]}"#)])
        let agents = try await client(host).agents()
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/agents")
        #expect(request.headers["Authorization"] == "Bearer wc_pat_x")
        let agent = try #require(agents.first)
        #expect(agent.id == "ag_1")
        #expect(agent.slug == "notes")
        #expect(agent.displayName == "Notes")
        #expect(agent.callType == "one-shot")
        #expect(agent.isOneWay)
        #expect(agent.position == 2)
        #expect(agent.stt == .object(["provider": .string("demo-stt")]))
        #expect(agent.action?["url"] == .string("https://example.com/hook"))
        #expect(agent.tts == nil)
        #expect(agent.vad == ["threshold": .double(0.5)])
        #expect(agent.timeouts == ["idle_s": .int(30)])
        #expect(agent.retentionDays == .null)
        #expect(agent.effectiveRetentionDays == .int(30))
        #expect(agent.createdAt == 1.5)
        #expect(agent.updatedAt == 2.5)
    }

    @Test func agentKeepsSnakeCaseKeysInsideEndpoints() async throws {
        let json = Self.agentJSON.replacingOccurrences(of: #"{"provider":"demo-stt"}"#, with: #"{"type":"x","base_url":"https://a.test"}"#)
        let host = StubHost(replies: [(200, json)])
        let agent = try await client(host).agent("notes")
        #expect(agent.stt?["base_url"] == .string("https://a.test"))
    }

    @Test func retentionKeepsItsKind() async throws {
        for (raw, expected) in [("30", JSONValue.int(30)), (#""forever""#, .string("forever")), ("null", .null)] {
            let json = Self.agentJSON.replacingOccurrences(of: #""retention_days":null"#, with: #""retention_days":\#(raw)"#)
            let host = StubHost(replies: [(200, json)])
            #expect(try await client(host).agent("notes").retentionDays == expected)
        }
    }

    @Test func conversationAgentIsNotOneWay() async throws {
        let json = Self.agentJSON.replacingOccurrences(of: "one-shot", with: "conversation")
        let host = StubHost(replies: [(200, json)])
        #expect(try await client(host).agent("notes").isOneWay == false)
    }

    @Test func agentGetsOneByReference() async throws {
        let host = StubHost(replies: [(200, Self.agentJSON)])
        _ = try await client(host).agent("notes")
        #expect(host.requests.first?.method == "GET")
        #expect(host.requests.first?.path == "/v1/agents/notes")
    }

    @Test func createAgentPostsTheFields() async throws {
        let host = StubHost(replies: [(201, Self.agentJSON)])
        let created = try await client(host).createAgent([
            "slug": .string("notes"),
            "action": .object(["type": .string("webhook"), "url": .string("https://example.com/hook")]),
            "retention_days": .int(30),
        ])
        #expect(created.slug == "notes")
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/agents")
        #expect(request.headers["Content-Type"] == "application/json")
        let body = try request.json()
        #expect(body["slug"] as? String == "notes")
        #expect((body["action"] as? [String: Any])?["url"] as? String == "https://example.com/hook")
        #expect(body["retention_days"] as? Int == 30)
    }

    @Test func updateAgentPatchesOnlyTheGivenKeys() async throws {
        let host = StubHost(replies: [(200, Self.agentJSON)])
        _ = try await client(host).updateAgent("notes", ["display_name": .string("Renamed"), "tts": .null])
        let request = try #require(host.requests.first)
        #expect(request.method == "PATCH")
        #expect(request.path == "/v1/agents/notes")
        let body = try request.json()
        #expect(Set(body.keys) == ["display_name", "tts"])
        #expect(body["tts"] is NSNull)
    }

    @Test func deleteAgentSendsDelete() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deleteAgent("notes")
        #expect(host.requests.first?.method == "DELETE")
        #expect(host.requests.first?.path == "/v1/agents/notes")
    }

    @Test func agentIdIsOnePathComponent() async throws {
        let host = StubHost(replies: [(204, "")])
        let client = ManagementClient(server: host.url, token: "wc_pat_x", session: .stubbed())
        try await client.deleteAgent("../devices")
        #expect(host.requests.first?.url.absoluteString.hasSuffix("/v1/agents/..%2Fdevices") == true)
    }

    @Test func deviceIdIsOnePathComponent() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).revokeDevice("a/b")
        #expect(host.requests.first?.url.absoluteString.hasSuffix("/v1/devices/a%2Fb") == true)
    }

    @Test func requestIDIsOnePathComponent() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deny(requestID: "1/2")
        #expect(host.requests.first?.url.absoluteString.hasSuffix("/v1/pairing-requests/1%2F2/deny") == true)
    }

    @Test func agentInvalidKeepsMessage() async {
        let host = StubHost(replies: [(422, #"{"error":"invalid","message":"slug: use 1 to 32 lowercase letters"}"#)])
        await #expect(throws: APIError.invalid("slug: use 1 to 32 lowercase letters")) {
            try await client(host).createAgent(["slug": .string("A")])
        }
    }

    @Test func agentConflictKeepsCodeAndMessage() async {
        let host = StubHost(replies: [(409, #"{"error":"conflict","message":"slug already in use"}"#)])
        await #expect(throws: APIError.conflict(code: "conflict", message: "slug already in use")) {
            try await client(host).createAgent(["slug": .string("notes")])
        }
    }

    @Test func missingAgentIsNotFound() async {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"agent not found: x"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).agent("x") }
    }

    // MARK: - Devices

    @Test func devicesDecode() async throws {
        let host = StubHost(replies: [(200, #"{"devices":[{"id":"d1","name":"Watch","created_at":10.5}]}"#)])
        let devices = try await client(host).devices()
        #expect(devices == [DeviceRecord(id: "d1", name: "Watch", createdAt: 10.5)])
        #expect(host.requests.first?.path == "/v1/devices")
    }

    @Test func revokeDeviceSendsDelete() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).revokeDevice("d1")
        #expect(host.requests.first?.method == "DELETE")
        #expect(host.requests.first?.path == "/v1/devices/d1")
    }

    @Test func revokeUnknownDeviceIsNotFound() async {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"device not found: d9"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).revokeDevice("d9") }
    }

    // MARK: - Pairing codes and approvals

    @Test func createPairingCodeDecodes() async throws {
        let host = StubHost(replies: [(201, #"{"code":"12345678","expires_at":1000.0,"server_url":"https://s.test","via_directory":true,"warning":"slow"}"#)])
        let grant = try await client(host).createPairingCode()
        #expect(grant.code == "12345678")
        #expect(grant.expiresAt == 1000)
        #expect(grant.serverUrl == "https://s.test")
        #expect(grant.viaDirectory)
        #expect(grant.warning == "slow")
        #expect(host.requests.first?.method == "POST")
        #expect(host.requests.first?.path == "/v1/pairing-codes")
    }

    @Test func pairingCodeGrantHidesTheCode() async throws {
        let host = StubHost(replies: [(201, #"{"code":"12345678","expires_at":1000.0,"server_url":"https://s.test","via_directory":false}"#)])
        let grant = try await client(host).createPairingCode()
        #expect(grant.warning == nil)
        for text in [String(describing: grant), String(reflecting: grant)] {
            #expect(!text.contains("12345678"))
        }
    }

    @Test func limitOnCreatePairingCode() async {
        let host = StubHost(replies: [(403, #"{"error":"limit","message":"device limit reached"}"#)])
        await #expect(throws: APIError.limit("device limit reached")) { try await client(host).createPairingCode() }
    }

    @Test func directoryFailureIsUnavailable() async {
        let host = StubHost(replies: [(502, #"{"error":"directory","message":"directory unreachable"}"#)])
        await #expect(throws: APIError.unavailable("directory unreachable")) { try await client(host).createPairingCode() }
    }

    @Test func pairingRequestsDecode() async throws {
        let host = StubHost(replies: [(200, #"{"requests":[{"request_id":"4821","device_name":"Watch","expires_at":99.5}]}"#)])
        let requests = try await client(host).pairingRequests()
        #expect(requests == [ApprovalRequest(requestId: "4821", deviceName: "Watch", expiresAt: 99.5)])
        #expect(requests.first?.id == "4821")
        #expect(host.requests.first?.path == "/v1/pairing-requests")
    }

    @Test func notConfiguredOnRequests() async {
        let host = StubHost(replies: [(404, #"{"error":"not_configured","message":"this server is not linked to a central account"}"#)])
        await #expect(throws: APIError.notConfigured) { try await client(host).pairingRequests() }
    }

    @Test func approveReturnsTheDeviceName() async throws {
        let host = StubHost(replies: [(200, #"{"device_name":"Watch"}"#)])
        let name = try await client(host).approve(requestID: "4821")
        #expect(name == "Watch")
        #expect(host.requests.first?.method == "POST")
        #expect(host.requests.first?.path == "/v1/pairing-requests/4821/approve")
    }

    @Test func limitOnApprove() async {
        let host = StubHost(replies: [(403, #"{"error":"limit","message":"device limit reached"}"#)])
        await #expect(throws: APIError.limit("device limit reached")) { try await client(host).approve(requestID: "4821") }
    }

    @Test func approveOfAnExpiredRequestIsNotFound() async {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"no pending request with this id"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).approve(requestID: "4821") }
    }

    @Test func denySendsPost() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deny(requestID: "4821")
        #expect(host.requests.first?.method == "POST")
        #expect(host.requests.first?.path == "/v1/pairing-requests/4821/deny")
    }

    // MARK: - Account link

    @Test func linkAccountWithTokenSendsBothProofs() async throws {
        let host = StubHost(replies: [(200, #"{"linked":true,"issuer":"https://cloud.test"}"#)])
        let link = try await client(host).linkAccount(serverToken: "jwt")
        #expect(link.linked)
        #expect(link.issuer == "https://cloud.test")
        #expect(link.user == nil)
        #expect(link.apiToken == nil)
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/account/link")
        #expect(request.headers["Authorization"] == "Bearer wc_pat_x")
        #expect(try request.json()["token"] as? String == "jwt")
        #expect(try request.json()["code"] == nil)
    }

    @Test func linkWithCodeSendsNoAuthorization() async throws {
        let host = StubHost(replies: [(200, #"{"linked":true,"issuer":"https://cloud.test","user":{"id":"u_1","handle":"gus"},"api_token":"wc_pat_new"}"#)])
        let link = try await ManagementClient.linkAccount(
            server: host.url, serverToken: "jwt", code: PairingCode("12345678")!, session: .stubbed())
        #expect(link.apiToken == "wc_pat_new")
        #expect(link.user == AccountLink.LinkedUser(id: "u_1", handle: "gus"))
        let request = try #require(host.requests.first)
        #expect(request.path == "/v1/account/link")
        #expect(request.headers["Authorization"] == nil)
        #expect(try request.json()["code"] as? String == "12345678")
        #expect(try request.json()["token"] as? String == "jwt")
    }

    @Test func accountLinkHidesTheAPIToken() async throws {
        let host = StubHost(replies: [(200, #"{"linked":true,"issuer":"https://c","user":{"id":"u","handle":"h"},"api_token":"wc_pat_secret"}"#)])
        let link = try await ManagementClient.linkAccount(
            server: host.url, serverToken: "jwt", code: PairingCode("12345678")!, session: .stubbed())
        for text in [String(describing: link), String(reflecting: link)] {
            #expect(!text.contains("wc_pat_secret"))
        }
    }

    @Test func linkWithAWrongCodeIsInvalidCode() async {
        let host = StubHost(replies: [(401, #"{"error":"invalid_code","message":"wrong or expired code"}"#)])
        await #expect(throws: APIError.invalidCode) {
            try await ManagementClient.linkAccount(
                server: host.url, serverToken: "jwt", code: PairingCode("12345678")!, session: .stubbed())
        }
    }

    @Test func linkWithABadAccountTokenIsInvalidAccountToken() async {
        let host = StubHost(replies: [(401, #"{"error":"invalid_account_token","message":"bad"}"#)])
        await #expect(throws: APIError.invalidAccountToken) { try await client(host).linkAccount(serverToken: "jwt") }
    }

    @Test func validationDetailBecomesInvalid() async {
        let host = StubHost(replies: [(422, #"{"detail":[{"loc":["body","code"],"msg":"Field required"}]}"#)])
        await #expect(throws: APIError.invalid("Field required")) {
            try await ManagementClient.linkAccount(
                server: host.url, serverToken: "jwt", code: PairingCode("12345678")!, session: .stubbed())
        }
    }

    @Test func linkRateLimited() async {
        let host = StubHost(replies: [(429, #"{"error":"rate_limited","message":"too many attempts"}"#)])
        await #expect(throws: APIError.rateLimited) { try await client(host).linkAccount(serverToken: "jwt") }
    }

    @Test func unlinkAccountSendsDelete() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).unlinkAccount()
        #expect(host.requests.first?.method == "DELETE")
        #expect(host.requests.first?.path == "/v1/account/link")
        #expect(host.requests.first?.headers["Authorization"] == "Bearer wc_pat_x")
    }

    @Test func unlinkWhenNotLinkedIsNotFound() async {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"this user is not linked to a central account"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).unlinkAccount() }
    }

    // MARK: - Errors

    @Test func statusMapping() {
        #expect(APIError.error(status: 401, data: Data(#"{"error":"unauthorized","message":"x"}"#.utf8)) == .unauthorized)
        #expect(APIError.error(status: 401, data: Data()) == .unauthorized)
        #expect(APIError.error(status: 401, data: Data("not json".utf8)) == .unauthorized)
        #expect(APIError.error(status: 403, data: Data(#"{"error":"not_linked","message":"link first"}"#.utf8)) == .notLinked("link first"))
        #expect(APIError.error(status: 403, data: Data()) == .forbidden("Not allowed with this token."))
        #expect(APIError.error(status: 404, data: Data()) == .notFound)
        #expect(APIError.error(status: 409, data: Data(#"{"error":"conflict"}"#.utf8)).isConflict(code: "conflict"))
        #expect(APIError.error(status: 422, data: Data()) == .invalid("The server rejected the request."))
        #expect(APIError.error(status: 503, data: Data(#"{"error":"account_unavailable","message":"later"}"#.utf8)) == .unavailable("later"))
        #expect(APIError.error(status: 500, data: Data()) == .unexpectedStatus(500))
    }

    @Test func conflictKeepsCode() {
        let error = APIError.error(status: 409, data: Data(#"{"error":"conflict"}"#.utf8))
        guard case .conflict(let code, _) = error else {
            Issue.record("expected .conflict, got \(error)")
            return
        }
        #expect(code == "conflict")
    }

    @Test func malformedSuccessBody() async {
        let host = StubHost(replies: [(200, #"{"unexpected":1}"#)])
        await #expect(throws: APIError.malformedResponse) { try await client(host).devices() }
    }

    @Test func unexpectedStatusIsReported() async {
        let host = StubHost(replies: [(500, "oops")])
        await #expect(throws: APIError.unexpectedStatus(500)) { try await client(host).devices() }
    }

    @Test func networkFailureIsReported() async {
        // A host nobody registered: the stub protocol answers `cannotFindHost`.
        let client = ManagementClient(server: URL(string: "https://nobody.stub.test")!, token: "wc_pat_x", session: .stubbed())
        await #expect(throws: APIError.network(.cannotFindHost)) { try await client.devices() }
    }

    @Test func messagesAreShortAndNeverEmpty() {
        let all: [APIError] = [
            .unauthorized, .invalidCode, .invalidAccountToken, .forbidden("x"), .limit("x"), .notLinked("x"),
            .notFound, .notConfigured, .conflict(code: "c", message: "x"), .invalid("x"), .rateLimited,
            .unavailable("x"), .unexpectedStatus(500), .network(.timedOut), .malformedResponse,
        ]
        for error in all {
            #expect(!error.message.isEmpty)
        }
        #expect(APIError.limit("device limit reached").message == "device limit reached")
    }

    @Test func tokenHelpers() {
        #expect(ManagementClient.isPersonalToken("wc_pat_abc"))
        #expect(ManagementClient.isPersonalToken("  wc_pat_abc\n"))
        #expect(!ManagementClient.isPersonalToken("abc"))
        #expect(ManagementClient.deviceTokenMessage == "This is a device token, not a personal token. Create one with `wristcall users tokens add`.")
    }

    @Test func clientDescriptionHidesTheToken() {
        let client = ManagementClient(server: URL(string: "https://s.test")!, token: "wc_pat_secret", session: .stubbed())
        for text in [String(describing: client), String(reflecting: client)] {
            #expect(!text.contains("wc_pat_secret"))
        }
    }
}

private extension APIError {
    func isConflict(code expected: String) -> Bool {
        if case .conflict(let code, _) = self { return code == expected }
        return false
    }
}
