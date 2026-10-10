#if os(macOS)
import Foundation
import Testing
import WristcallKit

/// The account flows end to end: a local wristcall Cloud, a local server whose central account is that Cloud,
/// and the real Zitadel behind it, where a human test user signs in through a browser driven by Playwright
/// (`tools/e2e/zitadel_browser.py`). Skipped unless `WRISTCALL_TEST_CLOUD` is set; the dev repo's E7 `e2e.sh`
/// starts everything and sets:
///
/// - `WRISTCALL_TEST_CLOUD`: the local Cloud (`http://127.0.0.1:8090`).
/// - `WRISTCALL_TEST_SERVER`, `WRISTCALL_TEST_CONFIG`, `WRISTCALL_TEST_CLI`: the local server and its CLI (see
///   `TestServer`). Its `central_account` is the Cloud above, `device_credential: approval`.
/// - `WRISTCALL_TEST_ACCOUNT_USER`: the server user the account links to (default `e7`).
/// - `WRISTCALL_TEST_BROWSER`: the helper command, e.g. `<venv>/bin/python tools/e2e/zitadel_browser.py`, with
///   its own `E2E_ZITADEL_*` settings. It gets the authorization URL or the user code on standard input.
/// - `WRISTCALL_TEST_FAKE_APNS_LOG`: the JSON file where the Cloud's fake APNs writes what it received.
///
/// Every login happens once per run (`AccountWorld`); the suite is serialized so only one browser runs at a time.
/// Nothing here prints a token, a code or a password; the `aud` and `client_id` claims are printed on purpose
/// (they are what the run is meant to observe).
@Suite(.serialized, .enabled(if: AccountEnvironment.isConfigured, "set WRISTCALL_TEST_CLOUD to run the account tests"))
struct AccountIntegrationTests {
    static let redirect = URL(string: "wristcall://auth/callback")!
    static let logoutRedirect = URL(string: "wristcall://auth/logout")!
    static let phoneTopic = "io.github.ggondim.wristcall"

    @Test func iosPKCELoginLinksServer() async throws {
        let login = try await AccountWorld.shared.phone()
        let cloud = try AccountEnvironment.requireCloud()

        // The Cloud accepts the human login's access token (its `aud` was the open question of E6).
        let token = try await login.session.accessToken()
        let (status, body) = try await AccountEnvironment.get(cloud.appending(path: "v1/account"), bearer: token)
        #expect(status == 200, "GET /v1/account answered \(status)")
        let account = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        #expect((account?["account"] as? String)?.isEmpty == false)
        // Secrets never go inside #expect/#require: a failure prints the values of the expression's parts.
        let parsed = JWTClaims(token)
        let claims = try #require(parsed)
        AccountEnvironment.report("aud ios-pkce: \(claims.audience) client_id: \(claims.clientID ?? "-")")
        #expect(claims.clientID == (try await login.session.config()).clients.ios)

        // `wristcall pair --user e7` + the Cloud's token for this server → a personal token.
        let link = try await AccountWorld.shared.link()
        #expect(link.via == .ios)
        #expect(link.account.linked)
        #expect(link.account.user?.handle == AccountEnvironment.serverUser)
        let personal = try #require(link.account.apiToken)
        let isPersonal = ManagementClient.isPersonalToken(personal)
        #expect(isPersonal)
        try await ManagementClient(server: try TestServer.requireBaseURL(), token: personal).verify()

        // End session: does Zitadel take the request without `id_token_hint`? Observed, not asserted.
        let (client, provider) = try await login.session.oidc()
        for hint in [nil, login.idToken] {
            guard let url = client.endSessionURL(provider, postLogoutRedirect: Self.logoutRedirect, idTokenHint: hint) else {
                AccountEnvironment.report("end-session: the provider has no end_session_endpoint")
                break
            }
            let reply = try await AccountEnvironment.firstHop(url)
            AccountEnvironment.report("end-session \(hint == nil ? "without" : "with") id_token_hint: \(reply)")
        }
    }

    @Test func iosAgendaRoundTrip() async throws {
        let session = try await AccountWorld.shared.phoneOrWatch()
        let server = try TestServer.requireBaseURL()
        let cloud = session.cloudClient
        let token = try await session.accessToken()

        let added = try await cloud.addServer(name: "E2E local", url: server, linked: true, accessToken: token)
        // Adding the same address again finds the entry (409 → existing one).
        let again = try await cloud.addServer(name: "E2E local", url: server, linked: true, accessToken: token)
        #expect(again.id == added.id)

        let agent = CloudAgent(id: "agt_e2e", slug: "e2e-note", displayName: "E2E Note", icon: "note", callType: "one-shot")
        try await cloud.setAgents([agent], serverID: added.id, accessToken: token)
        let agenda = try await cloud.servers(accessToken: token)
        let listed = try #require(agenda.first { $0.id == added.id })
        #expect(listed.name == "E2E local")
        #expect(listed.linked)
        #expect(listed.agents == [agent])
        #expect(ServerAddress.canonical(try #require(URL(string: listed.url))) == ServerAddress.canonical(server))
        // What the watch would offer after its own login.
        #expect(AccountPairing.candidates([listed], paired: []).map(ServerAddress.canonical) == [ServerAddress.canonical(server)])

        let renamed = try await cloud.updateServer(id: added.id, name: "E2E local 2", linked: nil, accessToken: token)
        #expect(renamed.name == "E2E local 2")
        try await cloud.deleteServer(id: added.id, accessToken: token)
        let after = try await cloud.servers(accessToken: token)
        #expect(after.contains { $0.id == added.id } == false)
    }

    @Test func watchDeviceFlowPairsWithApproval() async throws {
        let watch = try await AccountWorld.shared.watch()
        let parsed = JWTClaims(try await watch.accessToken())
        let claims = try #require(parsed)
        AccountEnvironment.report("aud watch-device-flow: \(claims.audience) client_id: \(claims.clientID ?? "-")")
        #expect(claims.clientID == (try await watch.config()).clients.watch)

        let link = try await AccountWorld.shared.link()
        let personal = try #require(link.account.apiToken)
        let server = try TestServer.requireBaseURL()

        // The watch asks with the Cloud's token for this server; `device_credential: approval` → pending.
        let outcome = await AccountPairing(session: watch).pair(server, deviceName: "E2E Watch")
        guard case .pending(_, let request) = outcome else {
            Issue.record("expected .pending, got \(outcome)")
            return
        }
        let management = ManagementClient(server: server, token: personal)
        let waiting = try await management.pairingRequests()
        #expect(waiting.contains { $0.requestId == request.requestId && $0.deviceName == "E2E Watch" })
        #expect(try await management.approve(requestID: request.requestId) == "E2E Watch")

        let pairing = PairingClient()
        guard case .paired(let device) = try await pairing.poll(server: server, pollToken: request.pollToken) else {
            Issue.record("the approved request did not hand over a device token")
            return
        }
        let me = try await pairing.me(server: server, token: device.token)
        #expect(me.deviceName == "E2E Watch")
        #expect(me.user?.handle == AccountEnvironment.serverUser)
        // The token is delivered once.
        let second = try await pairing.poll(server: server, pollToken: request.pollToken)
        #expect(second == .gone)
        try await management.revokeDevice(device.deviceId)
    }

    @Test func deviceApprovalPushReachesFakeAPNs() async throws {
        let log = try #require(AccountEnvironment.fakeAPNsLog, "set WRISTCALL_TEST_FAKE_APNS_LOG")
        let cloud = try AccountEnvironment.requireCloud()
        let server = try TestServer.requireBaseURL()
        let link = try await AccountWorld.shared.link()
        let personal = try #require(link.account.apiToken)
        let watch = try await AccountWorld.shared.watch()

        // The server relays through the local Cloud.
        let health = try await ServerPushClient.health(of: server)
        #expect(health.relay.map(ServerAddress.canonical) == ServerAddress.canonical(cloud))

        // The phone's registration: a fake APNs token, approval events only, tag = its own id for the server.
        let apnsToken = Data((0..<32).map { _ in UInt8.random(in: 0...255) })
        let tag = "srv-e2e-\(UInt32.random(in: 0...UInt32.max))"
        let relay = PushRelayClient(relayURL: cloud)
        let pushKey = try await relay.register(
            deviceToken: apnsToken, topic: Self.phoneTopic, environment: .sandbox, label: "E2E local", tag: tag,
            events: ["device.approval"]
        )
        let serverPush = ServerPushClient(server: server, token: personal)
        try await serverPush.setPushKey(pushKey)

        let outcome = await AccountPairing(session: watch).pair(server, deviceName: "E2E Push Watch")
        guard case .pending(_, let request) = outcome else {
            Issue.record("expected .pending, got \(outcome)")
            return
        }

        let hexToken = apnsToken.map { String(format: "%02x", $0) }.joined()
        let received = await FakeAPNs.waitForPush(in: URL(fileURLWithPath: log), deviceToken: hexToken)
        let push = try #require(received, "the fake APNs got nothing for this device token")
        #expect(push.topic == Self.phoneTopic)
        #expect(push.category == "WC_DEVICE_APPROVAL")
        #expect(push.event == "device.approval")
        #expect(push.tag == tag)
        #expect(push.requestID == request.requestId)
        #expect(push.requestID.count == 4 && push.requestID.allSatisfy(\.isASCII) && push.requestID.allSatisfy(\.isNumber))

        // Leave nothing behind: the request, the server's key and the relay's registration.
        try await ManagementClient(server: server, token: personal).deny(requestID: request.requestId)
        try await serverPush.clearPushKey()
        try await relay.unregister(pushKey: pushKey)
    }
}

// MARK: - Environment

enum AccountEnvironment {
    static let environment = ProcessInfo.processInfo.environment
    static let cloud = environment["WRISTCALL_TEST_CLOUD"].flatMap { URL(string: $0) }
    static var isConfigured: Bool { cloud != nil }
    static let serverUser = environment["WRISTCALL_TEST_ACCOUNT_USER"] ?? "e7"
    static let browser = environment["WRISTCALL_TEST_BROWSER"]
    static let fakeAPNsLog = environment["WRISTCALL_TEST_FAKE_APNS_LOG"]

    struct Failure: Error, CustomStringConvertible {
        let description: String
    }

    static func requireCloud() throws -> URL {
        guard let cloud else { throw Failure(description: "WRISTCALL_TEST_CLOUD not set") }
        return cloud
    }

    /// One line the e2e script collects (`[account-e2e] ...`). Never pass a secret.
    static func report(_ line: String) {
        print("[account-e2e] \(line)")
    }

    static func get(_ url: URL, bearer: String) async throws -> (Int, Data) {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        let (data, response) = try await URLSession.shared.data(for: request)
        return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
    }

    /// The provider's first answer to `url`, without following redirects: status and where it sends the
    /// browser (scheme, host and path only: the query may carry tokens).
    static func firstHop(_ url: URL) async throws -> String {
        let session = URLSession(configuration: .ephemeral, delegate: NoRedirects(), delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 15)
        request.setValue("wristcall-e2e", forHTTPHeaderField: "User-Agent")
        let (_, response) = try await session.data(for: request)
        let http = response as? HTTPURLResponse
        let location = http?.value(forHTTPHeaderField: "Location").flatMap { URLComponents(string: $0) }
        let target = location.map { "\($0.scheme ?? ""):\($0.host.map { "//\($0)" } ?? "")\($0.path)" } ?? "-"
        return "\(http?.statusCode ?? 0) → \(target)"
    }

    private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
        func urlSession(
            _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
            newRequest request: URLRequest
        ) async -> URLRequest? {
            nil
        }
    }
}

/// The claims this suite reads from an access token (JWT payload, not verified: the Cloud verified it).
struct JWTClaims {
    var audience: [String]
    var clientID: String?

    init?(_ token: String) {
        let parts = token.split(separator: ".")
        guard parts.count == 3 else { return nil }
        var base64 = parts[1].replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        guard let data = Data(base64Encoded: base64),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        if let list = object["aud"] as? [String] {
            audience = list
        } else if let one = object["aud"] as? String {
            audience = [one]
        } else {
            audience = []
        }
        clientID = (object["client_id"] as? String) ?? (object["azp"] as? String)
    }
}

// MARK: - Logins, once per run

/// The logins and the link, each done once per test process and shared by the tests.
actor AccountWorld {
    static let shared = AccountWorld()

    struct PhoneLogin: Sendable {
        let session: AccountSession
        /// The login's ID token, only for the end-session observation. A secret: never print it.
        let idToken: String?
    }

    struct Link: Sendable {
        enum Via: Sendable { case ios, watch }
        let account: AccountLink
        /// Which login asked the Cloud for the server token (the phone's, unless its login failed).
        let via: Via
    }

    private var phoneTask: Task<PhoneLogin, any Error>?
    private var watchTask: Task<AccountSession, any Error>?
    private var linkTask: Task<Link, any Error>?

    /// The iPhone's login: Authorization Code with PKCE through the browser helper.
    func phone() async throws -> PhoneLogin {
        if phoneTask == nil {
            phoneTask = Task { try await Self.phoneLogin() }
        }
        return try await phoneTask!.value
    }

    /// The watch's login: device authorization grant, the helper approving the user code.
    func watch() async throws -> AccountSession {
        if watchTask == nil {
            watchTask = Task { try await Self.watchLogin() }
        }
        return try await watchTask!.value
    }

    /// The phone's session, or the watch's when the phone login failed (the agenda routes take either; reported,
    /// and `iosPKCELoginLinksServer` fails on its own then).
    func phoneOrWatch() async throws -> AccountSession {
        do {
            return try await phone().session
        } catch {
            AccountEnvironment.report("agenda: the phone login failed (\(error)); using the watch login")
            return try await watch()
        }
    }

    /// Links the server user to the account: `wristcall pair --user <user>` + the Cloud's token for this server
    /// → `POST /v1/account/link` → a personal token. Uses the phone's login, as the app does; when that login
    /// fails, the watch's (the server cannot tell them apart), so the watch and push tests still run.
    func link() async throws -> Link {
        if linkTask == nil {
            linkTask = Task {
                do {
                    let phone = try await self.phone()
                    return Link(account: try await Self.link(phone.session), via: .ios)
                } catch {
                    AccountEnvironment.report("link: the phone login failed (\(error)); linking with the watch login")
                    return Link(account: try await Self.link(try await self.watch()), via: .watch)
                }
            }
        }
        return try await linkTask!.value
    }

    private static func phoneLogin() async throws -> PhoneLogin {
        let session = AccountSession(cloud: try AccountEnvironment.requireCloud(), kind: .ios, store: InMemoryTokenStore())
        let config = try await session.config()
        let (client, provider) = try await session.oidc()
        let request = client.authorizationRequest(provider, redirectURI: AccountIntegrationTests.redirect, scopes: config.scopes)
        let output = try await Browser.run("login", input: request.url.absoluteString)
        guard let callback = URL(string: output) else {
            throw AccountEnvironment.Failure(description: "the browser helper printed no callback URL")
        }
        let tokens = try await client.exchange(callback: callback, for: request, provider)
        try await session.signIn(tokens)
        return PhoneLogin(session: session, idToken: tokens.idToken)
    }

    private static func watchLogin() async throws -> AccountSession {
        let session = AccountSession(cloud: try AccountEnvironment.requireCloud(), kind: .watch, store: InMemoryTokenStore())
        let config = try await session.config()
        let (client, provider) = try await session.oidc()
        let authorization = try await client.startDeviceAuthorization(provider, scopes: config.scopes)
        let tokens = try await withThrowingTaskGroup(of: TokenSet?.self) { group in
            group.addTask {
                _ = try await Browser.run("device", input: authorization.userCode, expecting: "approved")
                return nil
            }
            group.addTask { try await client.pollDeviceToken(authorization, provider) }
            var tokens: TokenSet?
            do {
                while let next = try await group.next() {
                    if let next { tokens = next }
                }
            } catch {
                group.cancelAll()
                throw error
            }
            return tokens
        }
        guard let tokens else { throw AccountEnvironment.Failure(description: "device flow ended without tokens") }
        try await session.signIn(tokens)
        return session
    }

    private static func link(_ session: AccountSession) async throws -> AccountLink {
        let server = try TestServer.requireBaseURL()
        let output = try TestServer.runCLI(["pair", "--user", AccountEnvironment.serverUser])
        guard let line = output.split(separator: "\n").first(where: { $0.hasPrefix("Pairing code:") }),
              let code = PairingCode(String(line.filter(\.isNumber)))
        else { throw AccountEnvironment.Failure(description: "wristcall pair printed no code") }
        let health = try await ServerPushClient.health(of: server)
        let serverToken = try await session.serverToken(for: server, health: health)
        return try await ManagementClient.linkAccount(server: server, serverToken: serverToken, code: code)
    }
}

// MARK: - Browser helper

/// Runs `WRISTCALL_TEST_BROWSER <mode> -` with the argument on standard input; returns its standard output.
/// Its standard error (page paths only) goes to the test's.
enum Browser {
    static func run(_ mode: String, input: String, expecting: String? = nil) async throws -> String {
        guard let command = AccountEnvironment.browser, !command.isEmpty else {
            throw AccountEnvironment.Failure(description: "WRISTCALL_TEST_BROWSER not set")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "\(command) \(mode) -"]
        let stdin = Pipe()
        let stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = FileHandle.standardError
        let status: Int32 = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
                do {
                    try process.run()
                } catch {
                    process.terminationHandler = nil
                    continuation.resume(throwing: error)
                    return
                }
                stdin.fileHandleForWriting.write(Data((input + "\n").utf8))
                try? stdin.fileHandleForWriting.close()
            }
        } onCancel: {
            if process.isRunning { process.terminate() }
        }
        let output = String(decoding: stdout.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard status == 0 else {
            let reason = status == 3 ? "the provider's login page is unavailable" : "exit \(status)"
            throw AccountEnvironment.Failure(description: "browser helper (\(mode)) failed: \(reason)")
        }
        if let expecting, output != expecting {
            throw AccountEnvironment.Failure(description: "browser helper (\(mode)) printed something else than \(expecting)")
        }
        return output
    }
}

// MARK: - Fake APNs

/// What the Cloud's fake APNs (in `e2e.sh`) wrote: one JSON object per request, `path`, `headers`, `body`.
enum FakeAPNs {
    struct Push {
        var topic: String?
        var category: String?
        var event: String?
        var tag: String?
        var requestID: String
    }

    static func waitForPush(in file: URL, deviceToken: String, attempts: Int = 40) async -> Push? {
        for _ in 0..<attempts {
            if let push = read(file, deviceToken: deviceToken) { return push }
            try? await Task.sleep(for: .milliseconds(250))
        }
        return nil
    }

    private static func read(_ file: URL, deviceToken: String) -> Push? {
        guard let data = try? Data(contentsOf: file),
              let entries = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]]
        else { return nil }
        for entry in entries.reversed() where (entry["path"] as? String) == "/3/device/\(deviceToken)" {
            let headers = entry["headers"] as? [String: Any]
            guard let body = (entry["body"] as? String).flatMap({ $0.data(using: .utf8) }),
                  let payload = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            else { continue }
            let aps = payload["aps"] as? [String: Any]
            let wristcall = payload["wristcall"] as? [String: Any]
            let data = wristcall?["data"] as? [String: Any]
            return Push(
                topic: headers?["apns-topic"] as? String,
                category: aps?["category"] as? String,
                event: wristcall?["event"] as? String,
                tag: wristcall?["tag"] as? String,
                requestID: data?["request_id"] as? String ?? ""
            )
        }
        return nil
    }
}
#endif
