import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct HistoryClientTests {
    private static let callJSON = #"""
    {"id":"call_1","agent_id":"ag_1","call_type":"one-shot","status":"failed","error":"delivery_failed",
     "text":"remember the milk","attempts":3,"last_http_status":500,"created_at":1700000000.5,"ended_at":1700000010.0,
     "finished_at":1700000020.0,"agent":{"id":"ag_1","slug":"notes","display_name":"Notes"},"expires_at":1702592000.0,
     "entries":[{"role":"user","text":"remember the milk","error":null,"at":1700000001.0},
                {"role":"agent","text":null,"error":"tts_failed","at":1700000002.0}]}
    """#

    private func client(_ host: StubHost) -> HistoryClient {
        HistoryClient(server: host.url, token: "wc_pat_x", session: .stubbed())
    }

    private func query(_ request: StubHost.Request) -> [String: String] {
        let items = URLComponents(url: request.url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.value ?? "") })
    }

    private func record(status: String = "failed", callType: String = "one-shot", error: String? = "delivery_failed",
                        text: String? = "hello") throws -> CallRecord {
        func literal(_ value: String?) -> String {
            guard let value, let data = try? JSONEncoder().encode(value) else { return "null" }
            return String(decoding: data, as: UTF8.self)
        }
        let errorJSON = literal(error)
        let textJSON = literal(text)
        let json = #"""
        {"id":"c","agent_id":"a","call_type":"\#(callType)","status":"\#(status)","error":\#(errorJSON),"text":\#(textJSON),
         "attempts":1,"last_http_status":null,"created_at":1.0,"ended_at":null,"finished_at":null,
         "agent":{"id":"a","slug":"s","display_name":"S"},"expires_at":null,"entries":[]}
        """#
        return try JSONDecoder().decode(CallRecord.self, from: Data(json.utf8))
    }

    // MARK: - List

    @Test func callsSendsQuery() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        let page = try await client(host).calls(HistoryQuery(
            agent: "notes", text: "milk", since: Date(timeIntervalSince1970: 1_700_000_000.9),
            until: Date(timeIntervalSince1970: 1_700_086_400), before: "1700000000.5:call_9", limit: 10))
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/calls")
        #expect(request.headers["Authorization"] == "Bearer wc_pat_x")
        #expect(query(request) == [
            "agent": "notes", "q": "milk", "since": "1700000000", "until": "1700086400",
            "before": "1700000000.5:call_9", "limit": "10",
        ])
        #expect(page.calls.isEmpty)
        #expect(page.nextBefore == nil)
    }

    @Test func defaultQueryOnlySendsTheLimit() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery())
        #expect(query(try #require(host.requests.first)) == ["limit": "30"])
    }

    @Test func blankSearchIsOmitted() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(text: "  \n\t "))
        #expect(query(try #require(host.requests.first))["q"] == nil)
    }

    @Test func searchIsTrimmed() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(text: "  grocery list "))
        #expect(query(try #require(host.requests.first))["q"] == "grocery list")
    }

    @Test func searchEncodesPlus() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(text: "c++ +55 11"))
        let request = try #require(host.requests.first)
        let raw = try #require(request.url.query(percentEncoded: true))
        #expect(raw.contains("q=c%2B%2B%20%2B55%2011"))
        #expect(!raw.contains("+"))
    }

    @Test func blankAgentIsOmitted() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#), (200, "{}")])
        let history = client(host)
        _ = try await history.calls(HistoryQuery(agent: "  "))
        _ = try await history.export(.json, agent: "")
        #expect(host.requests.allSatisfy { query($0)["agent"] == nil })
    }

    @Test func blankAgentIsNeverDeleteAll() async throws {
        let host = StubHost(replies: [(200, #"{"deleted":9}"#)])
        await #expect(throws: APIError.invalid("Choose an agent.")) { try await client(host).deleteCalls(agent: " ") }
        #expect(host.requests.isEmpty)
    }

    @Test func nonFiniteDatesAreSkipped() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(
            since: Date(timeIntervalSince1970: .infinity), until: Date(timeIntervalSince1970: .nan)))
        #expect(query(try #require(host.requests.first)) == ["limit": "30"])
    }

    @Test func longSearchIsCutAtWhatTheServerTakes() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(text: String(repeating: "a", count: 600)))
        #expect(query(try #require(host.requests.first))["q"]?.count == 500)
    }

    @Test(arguments: [(0, "1"), (-5, "1"), (1, "1"), (100, "100"), (101, "100"), (5000, "100")])
    func limitIsClamped(limit: Int, sent: String) async throws {
        let host = StubHost(replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery(limit: limit))
        #expect(query(try #require(host.requests.first))["limit"] == sent)
    }

    @Test func pageDecodes() async throws {
        let host = StubHost(replies: [(200, #"{"calls":[\#(Self.callJSON)],"next_before":"1700000000.5:call_1"}"#)])
        let page = try await client(host).calls(HistoryQuery())
        #expect(page.nextBefore == "1700000000.5:call_1")
        let call = try #require(page.calls.first)
        #expect(call.id == "call_1")
        #expect(call.agentId == "ag_1")
        #expect(call.callType == "one-shot")
        #expect(call.status == "failed")
        #expect(call.error == "delivery_failed")
        #expect(call.text == "remember the milk")
        #expect(call.attempts == 3)
        #expect(call.lastHttpStatus == 500)
        #expect(call.createdAt == 1_700_000_000.5)
        #expect(call.endedAt == 1_700_000_010)
        #expect(call.finishedAt == 1_700_000_020)
        #expect(call.expiresAt == 1_702_592_000)
        #expect(call.agent == CallRecord.AgentRef(id: "ag_1", slug: "notes", displayName: "Notes"))
        #expect(call.entries == [
            CallEntry(role: "user", text: "remember the milk", error: nil, at: 1_700_000_001),
            CallEntry(role: "agent", text: nil, error: "tts_failed", at: 1_700_000_002),
        ])
    }

    @Test func nullsDecode() async throws {
        let json = #"""
        {"calls":[{"id":"c","agent_id":"a","call_type":"conversation","status":"completed","error":null,"text":null,
         "attempts":0,"last_http_status":null,"created_at":1.0,"ended_at":null,"finished_at":null,
         "agent":{"id":"a","slug":"","display_name":""},"expires_at":null,"entries":[]}],"next_before":null}
        """#
        let page = try await client(StubHost(replies: [(200, json)])).calls(HistoryQuery())
        let call = try #require(page.calls.first)
        #expect(call.error == nil)
        #expect(call.text == nil)
        #expect(call.endedAt == nil)
        #expect(call.expiresAt == nil)
        #expect(call.lastHttpStatus == nil)
        #expect(call.entries.isEmpty)
    }

    @Test func listBodyThatIsNotAPageIsMalformed() async throws {
        let host = StubHost(replies: [(200, #"{"calls":"nope"}"#)])
        await #expect(throws: APIError.malformedResponse) { try await client(host).calls(HistoryQuery()) }
    }

    @Test func deviceTokenIsForbiddenOnTheList() async throws {
        let host = StubHost(replies: [(403, #"{"error":"forbidden","message":"this needs user API token, not device token"}"#)])
        await #expect(throws: APIError.forbidden("this needs user API token, not device token")) {
            try await client(host).calls(HistoryQuery())
        }
    }

    @Test func invalidCursorIsRejected() async throws {
        let host = StubHost(replies: [(422, #"{"error":"invalid","message":"before: pass the next_before of the previous page"}"#)])
        await #expect(throws: APIError.invalid("before: pass the next_before of the previous page")) {
            try await client(host).calls(HistoryQuery(before: "junk"))
        }
    }

    @Test func serverPathPrefixIsKept() async throws {
        let host = StubHost(path: "/wristcall", replies: [(200, #"{"calls":[],"next_before":null}"#)])
        _ = try await client(host).calls(HistoryQuery())
        #expect(host.requests.first?.path == "/wristcall/v1/calls")
    }

    // MARK: - One call

    @Test func callDecodesAndEscapesTheID() async throws {
        let host = StubHost(replies: [(200, Self.callJSON)])
        let call = try await client(host).call("a/b c")
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.url.absoluteString.hasSuffix("/v1/calls/a%2Fb%20c"))
        #expect(call.id == "call_1")
    }

    @Test func missingCallIsNotFound() async throws {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"call not found"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).call("x") }
    }

    // MARK: - Delete

    @Test func deleteCallAnswers204() async throws {
        let host = StubHost(replies: [(204, "")])
        try await client(host).deleteCall("call_1")
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/calls/call_1")
    }

    @Test func deleteCallOfAMissingCallIsNotFound() async throws {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"call not found"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).deleteCall("x") }
    }

    @Test func deleteAllByAgent() async throws {
        let host = StubHost(replies: [(200, #"{"deleted":4}"#)])
        let count = try await client(host).deleteCalls(agent: "notes")
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/calls")
        #expect(query(request) == ["agent": "notes"])
        #expect(count == 4)
    }

    @Test func deleteAllEverything() async throws {
        let host = StubHost(replies: [(200, #"{"deleted":12}"#)])
        let count = try await client(host).deleteCalls(agent: nil)
        let request = try #require(host.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/calls")
        #expect(query(request) == ["all": "true"])
        #expect(count == 12)
    }

    @Test func deleteAllOfAnUnknownAgentIsNotFound() async throws {
        let host = StubHost(replies: [(404, #"{"error":"not_found","message":"agent not found: x"}"#)])
        await #expect(throws: APIError.notFound) { try await client(host).deleteCalls(agent: "x") }
    }

    // MARK: - Redelivery

    @Test func redeliverAnswers202WithTheCall() async throws {
        let host = StubHost(replies: [(202, Self.callJSON.replacingOccurrences(of: #""status":"failed""#, with: #""status":"processing""#))])
        let call = try await client(host).redeliver("call_1")
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/calls/call_1/redeliver")
        #expect(call.status == "processing")
    }

    @Test func redeliverConflictKeepsReason() async throws {
        let host = StubHost(replies: [(409, #"{"error":"not_failed","message":"only a call whose delivery failed can be delivered again"}"#)])
        await #expect(throws: APIError.conflict(code: "not_failed", message: "only a call whose delivery failed can be delivered again")) {
            try await client(host).redeliver("call_1")
        }
    }

    @Test func canRedeliverOnlyFailedOneWay() throws {
        #expect(try record().canRedeliver)
        #expect(try record(callType: "monologue").canRedeliver)
        #expect(try record(error: "interrupted").canRedeliver)
        #expect(try !record(callType: "conversation").canRedeliver)
        #expect(try !record(status: "completed", error: nil).canRedeliver)
        #expect(try !record(status: "processing", error: nil).canRedeliver)
        #expect(try !record(error: "stt_failed").canRedeliver)
        #expect(try !record(error: nil).canRedeliver)
        #expect(try !record(text: nil).canRedeliver)
        #expect(try !record(text: "").canRedeliver)
        #expect(try !record(text: "  \n").canRedeliver)
    }

    // MARK: - Export

    @Test func exportUsesHeaderFilename() async throws {
        let host = StubHost(replies: [StubHost.Reply(
            200, "# History", headers: ["Content-Disposition": #"attachment; filename="wristcall-history-20261010-120000.md""#])])
        let export = try await client(host).export(.markdown)
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/calls/export")
        #expect(query(request) == ["format": "md"])
        #expect(request.headers["Authorization"] == "Bearer wc_pat_x")
        #expect(export.filename == "wristcall-history-20261010-120000.md")
        #expect(String(decoding: export.data, as: UTF8.self) == "# History")
    }

    @Test func exportSendsFiltersAndFormat() async throws {
        let host = StubHost(replies: [(200, #"{"version": 1}"#)])
        _ = try await client(host).export(
            .json, agent: "notes", since: Date(timeIntervalSince1970: 1_700_000_000.2),
            until: Date(timeIntervalSince1970: 1_700_086_400))
        #expect(query(try #require(host.requests.first)) == [
            "format": "json", "agent": "notes", "since": "1700000000", "until": "1700086400",
        ])
    }

    @Test(arguments: [
        (#"attachment; filename="../../x.md""#, "x.md"),
        (#"attachment; filename="..\..\evil.json""#, "evil.json"),
        (#"attachment; filename="/etc/pass wd.md""#, "passwd.md"),
        (#"attachment; filename="a b;c$d.md""#, "abcd.md"),
        ("attachment; filename=plain.md", "plain.md"),
        (#"attachment; filename="..""#, "wristcall-history.md"),
        (#"attachment; filename="""#, "wristcall-history.md"),
        (#"attachment; filename="dir/""#, "wristcall-history.md"),
        (#"attachment; filename*=UTF-8''x.md"#, "wristcall-history.md"),
        ("attachment", "wristcall-history.md"),
    ])
    func exportFilenameIsSanitized(header: String, expected: String) async throws {
        let host = StubHost(replies: [StubHost.Reply(200, "x", headers: ["Content-Disposition": header])])
        let export = try await client(host).export(.markdown)
        #expect(export.filename == expected)
    }

    @Test func exportWithoutHeaderUsesADefaultName() async throws {
        let md = try await client(StubHost(replies: [(200, "x")])).export(.markdown)
        let json = try await client(StubHost(replies: [(200, "{}")])).export(.json)
        #expect(md.filename == "wristcall-history.md")
        #expect(json.filename == "wristcall-history.json")
    }

    @Test func exportErrorsAreMapped() async throws {
        let host = StubHost(replies: [(422, #"{"error":"invalid","message":"format: md or json"}"#)])
        await #expect(throws: APIError.invalid("format: md or json")) { try await client(host).export(.json) }
    }

    // MARK: - Secrets

    @Test func descriptionHidesTheToken() {
        let text = "\(HistoryClient(server: URL(string: "https://example.com")!, token: "wc_pat_secret"))"
        #expect(!text.contains("wc_pat_secret"))
        #expect(!String(reflecting: HistoryClient(server: URL(string: "https://example.com")!, token: "wc_pat_secret"))
            .contains("wc_pat_secret"))
    }
}
