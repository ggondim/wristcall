import Foundation
import Testing
import WristcallKit

struct PairingClientTests {
    let sleeper = SleepRecorder()
    let code = PairingCode("1234 5678")!

    func client() -> PairingClient {
        PairingClient(session: .stubbed(), sleep: sleeper.sleep)
    }

    // MARK: - Directory

    @Test func resolveReturnsTheServerURL() async throws {
        let directory = StubHost(replies: [(200, #"{"url":"https://agent.example.com"}"#)])
        let server = try await client().resolve(code: code, directory: directory.url)
        #expect(server == URL(string: "https://agent.example.com"))
        let request = try #require(directory.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/resolve/12345678")
        #expect(sleeper.delays.isEmpty)
    }

    @Test func resolveKeepsTheDirectoryPathPrefix() async throws {
        let directory = StubHost(path: "/pair/", replies: [(200, #"{"url":"https://agent.example.com"}"#)])
        _ = try await client().resolve(code: code, directory: directory.url)
        #expect(directory.requests.first?.path == "/pair/v1/resolve/12345678")
    }

    @Test func resolveRetriesNotFoundTwoSecondsApart() async throws {
        let directory = StubHost(replies: [
            (404, #"{"error":"not_found"}"#),
            (404, #"{"error":"not_found"}"#),
            (200, #"{"url":"https://agent.example.com/base"}"#),
        ])
        let server = try await client().resolve(code: code, directory: directory.url)
        #expect(server == URL(string: "https://agent.example.com/base"))
        #expect(directory.requests.count == 3)
        #expect(sleeper.delays == [.seconds(2), .seconds(2)])
    }

    @Test func resolveGivesUpAfterThreeRetries() async throws {
        let directory = StubHost(replies: [(404, #"{"error":"not_found"}"#)])
        await #expect(throws: PairingError.codeNotFound) {
            try await client().resolve(code: code, directory: directory.url)
        }
        #expect(directory.requests.count == 4)
        #expect(sleeper.delays == [.seconds(2), .seconds(2), .seconds(2)])
    }

    @Test(arguments: ["http://agent.example.com", "ftp://agent.example.com", "https://", "agent.example.com"])
    func resolveRejectsServersThatAreNotHTTPS(url: String) async throws {
        let directory = StubHost(replies: [(200, #"{"url":"\#(url)"}"#)])
        await #expect(throws: PairingError.insecureServerURL) {
            try await client().resolve(code: code, directory: directory.url)
        }
    }

    @Test func resolveRejectsAMalformedReply() async throws {
        let directory = StubHost(replies: [(200, #"{"address":"https://agent.example.com"}"#)])
        await #expect(throws: PairingError.malformedResponse) {
            try await client().resolve(code: code, directory: directory.url)
        }
    }

    @Test(arguments: [(429, PairingError.rateLimited), (500, .unexpectedStatus(500)), (503, .unexpectedStatus(503))])
    func resolveDoesNotRetryOtherErrors(status: Int, expected: PairingError) async throws {
        let directory = StubHost(replies: [(status, #"{"error":"x"}"#)])
        await #expect(throws: expected) {
            try await client().resolve(code: code, directory: directory.url)
        }
        #expect(directory.requests.count == 1)
        #expect(sleeper.delays.isEmpty)
    }

    // MARK: - POST /v1/pair

    @Test func pairWithACodeReturnsTheToken() async throws {
        let server = StubHost(replies: [(200, #"{"device_id":"dev-1","token":"secret-token"}"#)])
        let result = try await client().pair(server: server.url, code: code, deviceName: "Test's Watch")
        #expect(result == .paired(PairedDevice(deviceId: "dev-1", token: "secret-token")))
        let request = try #require(server.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/pair")
        #expect(request.headers["Content-Type"] == "application/json")
        #expect(request.headers["Authorization"] == nil)
        let body = try request.json()
        #expect(body["code"] as? String == "12345678")
        #expect(body["device_name"] as? String == "Test's Watch")
    }

    @Test func pairWithoutACodeSendsNullAndWaitsForApproval() async throws {
        let server = StubHost(replies: [(202, #"{"request_id":"0042","poll_token":"poll-secret","expires_at":1760000000.5}"#)])
        let result = try await client().pair(server: server.url, code: nil, deviceName: "Apple Watch")
        let expected = PairingRequest(
            requestId: "0042",
            pollToken: "poll-secret",
            expiresAt: Date(timeIntervalSince1970: 1_760_000_000.5)
        )
        #expect(result == .pending(expected))
        let body = try #require(server.requests.first).json()
        #expect(body["code"] is NSNull)
        #expect(body.keys.sorted() == ["code", "device_name"])
    }

    @Test func pairKeepsTheServerPathPrefix() async throws {
        let server = StubHost(path: "/wristcall", replies: [(200, #"{"device_id":"d","token":"t"}"#)])
        _ = try await client().pair(server: server.url, code: code, deviceName: "Apple Watch")
        #expect(server.requests.first?.path == "/wristcall/v1/pair")
    }

    @Test(arguments: [
        (401, #"{"error":"invalid_code","message":"invalid or expired code"}"#, PairingError.invalidCode),
        (429, #"{"error":"rate_limited"}"#, .rateLimited),
        (422, #"{"detail":[]}"#, .invalidRequest),
        (500, "Internal Server Error", .unexpectedStatus(500)),
        (200, #"{"device_id":"d"}"#, .malformedResponse),
        (202, #"{"request_id":"0042"}"#, .malformedResponse),
    ])
    func pairErrors(status: Int, body: String, expected: PairingError) async throws {
        let server = StubHost(replies: [(status, body)])
        await #expect(throws: expected) {
            try await client().pair(server: server.url, code: code, deviceName: "Apple Watch")
        }
    }

    @Test func networkFailuresAreWrapped() async throws {
        let server = StubHost { _ in throw URLError(.notConnectedToInternet) }
        await #expect(throws: PairingError.network(.notConnectedToInternet)) {
            try await client().pair(server: server.url, code: code, deviceName: "Apple Watch")
        }
    }

    // MARK: - POST /v1/pair/poll

    @Test func pollSendsTheTokenInTheBodyOnly() async throws {
        let server = StubHost(replies: [(202, #"{"request_id":"0042","expires_at":1760000000.5}"#)])
        let result = try await client().poll(server: server.url, pollToken: "poll-secret")
        #expect(result == .pending(requestId: "0042"))
        let request = try #require(server.requests.first)
        #expect(request.method == "POST")
        #expect(request.path == "/v1/pair/poll")
        #expect(!request.url.absoluteString.contains("poll-secret"))
        #expect(try request.json()["poll_token"] as? String == "poll-secret")
    }

    @Test(arguments: [
        (200, #"{"device_id":"dev-2","token":"tok"}"#, PollResult.paired(PairedDevice(deviceId: "dev-2", token: "tok"))),
        (410, #"{"error":"gone","message":"request already delivered"}"#, .gone),
    ])
    func pollOutcomes(status: Int, body: String, expected: PollResult) async throws {
        let server = StubHost(replies: [(status, body)])
        #expect(try await client().poll(server: server.url, pollToken: "poll-secret") == expected)
    }

    @Test(arguments: [(422, PairingError.invalidRequest), (429, .rateLimited), (502, .unexpectedStatus(502))])
    func pollErrors(status: Int, expected: PairingError) async throws {
        let server = StubHost(replies: [(status, "{}")])
        await #expect(throws: expected) {
            try await client().poll(server: server.url, pollToken: "poll-secret")
        }
    }

    // MARK: - /v1/me

    @Test func meReturnsTheDeviceAndProfiles() async throws {
        let server = StubHost(replies: [(200, """
            {"device_id":"dev-1","device_name":"Apple Watch",
             "profiles":[{"name":"default","display_name":"Agent"},{"name":"demo","display_name":"Demo"}]}
            """)])
        let info = try await client().me(server: server.url, token: "secret-token")
        #expect(info == DeviceInfo(
            deviceId: "dev-1",
            deviceName: "Apple Watch",
            profiles: [Profile(name: "default", displayName: "Agent"), Profile(name: "demo", displayName: "Demo")]
        ))
        let request = try #require(server.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/me")
        #expect(request.headers["Authorization"] == "Bearer secret-token")
    }

    @Test func meFromServer050ReturnsTheUserAndAgents() async throws {
        let server = StubHost(replies: [(200, """
            {"device_id":"dev-1","device_name":"Apple Watch",
             "user":{"id":"u_1","handle":"ana","display_name":"Ana"},
             "agents":[
               {"id":"ag_1","slug":"default","display_name":"Agent","icon":"waveform","call_type":"conversation","turn_end":"auto"},
               {"id":"ag_2","slug":"note","display_name":"Note","icon":"note.text","call_type":"one-shot","turn_end":"manual"}],
             "profiles":[{"name":"default","display_name":"Agent"},{"name":"note","display_name":"Note"}]}
            """)])
        let info = try await client().me(server: server.url, token: "secret-token")
        #expect(info.user == UserInfo(id: "u_1", handle: "ana", displayName: "Ana"))
        #expect(info.agents == [
            Agent(id: "ag_1", slug: "default", displayName: "Agent"),
            Agent(id: "ag_2", slug: "note", displayName: "Note", icon: "note.text", callType: .oneShot, turnEnd: .manual),
        ])
        #expect(info.profiles == [Profile(name: "default", displayName: "Agent"), Profile(name: "note", displayName: "Note")])
    }

    @Test func meFromServer02xDerivesAgentsFromProfiles() async throws {
        let server = StubHost(replies: [(200, """
            {"device_id":"dev-1","device_name":"Apple Watch",
             "profiles":[{"name":"default","display_name":"Agent"},{"name":"demo","display_name":"Demo"}]}
            """)])
        let info = try await client().me(server: server.url, token: "secret-token")
        #expect(info.user == nil)
        #expect(info.agents == [
            Agent(profile: Profile(name: "default", displayName: "Agent")),
            Agent(profile: Profile(name: "demo", displayName: "Demo")),
        ])
    }

    @Test func meWithAgentsOnlyDerivesProfiles() async throws {
        let server = StubHost(replies: [(200, """
            {"device_id":"dev-1","device_name":"Apple Watch",
             "agents":[{"id":"ag_1","slug":"note","display_name":"Note"}]}
            """)])
        let info = try await client().me(server: server.url, token: "secret-token")
        #expect(info.profiles == [Profile(name: "note", displayName: "Note")])
    }

    @Test func meWithNeitherAgentsNorProfilesIsMalformed() async throws {
        let server = StubHost(replies: [(200, #"{"device_id":"dev-1","device_name":"Apple Watch"}"#)])
        await #expect(throws: PairingError.malformedResponse) {
            try await client().me(server: server.url, token: "secret-token")
        }
    }

    @Test func deviceInfoInitsDeriveTheOtherList() {
        let fromProfiles = DeviceInfo(deviceId: "d", deviceName: "W", profiles: [Profile(name: "a", displayName: "A")])
        #expect(fromProfiles.agents == [Agent(id: "a", slug: "a", displayName: "A")])
        #expect(fromProfiles.user == nil)
        let agent = Agent(id: "ag_1", slug: "note", displayName: "Note", icon: "note.text", callType: .oneShot)
        let fromAgents = DeviceInfo(deviceId: "d", deviceName: "W", user: UserInfo(id: "u", handle: "ana", displayName: nil), agents: [agent])
        #expect(fromAgents.profiles == [Profile(name: "note", displayName: "Note")])
        #expect(fromAgents.agents == [agent])
    }

    @Test func meWithARevokedTokenIsUnauthorized() async throws {
        let server = StubHost(replies: [(401, #"{"error":"unauthorized"}"#)])
        await #expect(throws: PairingError.unauthorized) {
            try await client().me(server: server.url, token: "revoked")
        }
    }

    @Test func unpairDeletesMe() async throws {
        let server = StubHost(replies: [(204, "")])
        try await client().unpair(server: server.url, token: "secret-token")
        let request = try #require(server.requests.first)
        #expect(request.method == "DELETE")
        #expect(request.path == "/v1/me")
        #expect(request.headers["Authorization"] == "Bearer secret-token")
    }

    @Test(arguments: [(401, PairingError.unauthorized), (500, .unexpectedStatus(500))])
    func unpairErrors(status: Int, expected: PairingError) async throws {
        let server = StubHost(replies: [(status, "{}")])
        await #expect(throws: expected) {
            try await client().unpair(server: server.url, token: "secret-token")
        }
    }

    // MARK: - Secrets stay out of logs

    @Test func secretsAreRedactedInDescriptions() {
        let request = PairingRequest(requestId: "0042", pollToken: "poll-secret", expiresAt: .now)
        let device = PairedDevice(deviceId: "dev-1", token: "secret-token")
        for text in [String(describing: request), String(reflecting: request), "\(PairResult.pending(request))"] {
            #expect(!text.contains("poll-secret"))
            #expect(text.contains("0042"))
        }
        for text in [String(describing: device), String(reflecting: device), "\(PollResult.paired(device))"] {
            #expect(!text.contains("secret-token"))
            #expect(text.contains("dev-1"))
        }
    }
}
