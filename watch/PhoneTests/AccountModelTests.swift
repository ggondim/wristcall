import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

@MainActor
struct AccountModelTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_800_000_000)

    let world = AccountWorld()
    var cloud: StubHost { world.cloud }
    var cloudHost: StubHost { world.cloud }
    let server = ManagedServer(id: "home", name: "Home", url: URL(string: "https://srv.test")!, token: "wc_pat_home")

    var fresh: TokenSet {
        TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: Self.clock.addingTimeInterval(3600), idToken: "idt")
    }
    var stale: TokenSet {
        TokenSet(accessToken: "old-at", refreshToken: "rt", expiresAt: Self.clock.addingTimeInterval(10))
    }

    /// The server's health naming this Cloud as its account.
    var ownHealth: ServerHealth {
        ServerHealth(version: "0.6.0", account: .init(issuer: world.cloud.url.absoluteString, deviceCredential: "approval"))
    }

    func session(_ store: InMemoryTokenStore) -> AccountSession {
        AccountSession(cloud: world.cloud.url, kind: .ios, store: store, session: .stubbed(), now: { Self.clock })
    }

    func signedInModel(
        api: FakeServerAPI = FakeServerAPI(),
        servers: [ManagedServer] = [],
        expiredAccessToken: Bool = false,
        store: InMemoryTokenStore? = nil,
        web: FakeWeb = .approving()
    ) async throws -> AccountModel {
        world.refuseRefresh = expiredAccessToken
        let tokens = store ?? InMemoryTokenStore(expiredAccessToken ? stale : fresh)
        let appState = AppState(store: InMemoryManagedServerStore(servers), makeAPI: { _, _ in api })
        let model = AccountModel(cloudURL: world.cloud.url, session: session(tokens), web: web, state: appState)
        await model.restore()
        return model
    }

    // MARK: - Availability

    @Test func unavailableWithoutCloudURL() async {
        let web = FakeWeb.approving()
        let model = AccountModel(cloudURL: nil, session: nil, web: web, state: AppState(store: InMemoryManagedServerStore()))
        #expect(model.state == .unavailable)
        await model.restore()
        await model.signIn()
        #expect(model.state == .unavailable)
        #expect(web.opened.isEmpty)
    }

    @Test func cloudURLComesOnlyFromASafeBuildValue() {
        #expect(AccountModel.cloudURL(fromInfoValue: nil) == nil)
        #expect(AccountModel.cloudURL(fromInfoValue: "") == nil)
        #expect(AccountModel.cloudURL(fromInfoValue: "$(WRISTCALL_CLOUD_URL)") == nil)
        #expect(AccountModel.cloudURL(fromInfoValue: "http://cloud.test") == nil)
        #expect(AccountModel.cloudURL(fromInfoValue: "https://cloud.test")?.absoluteString == "https://cloud.test")
        #expect(AccountModel.cloudURL(fromInfoValue: "http://127.0.0.1:8090")?.absoluteString == "http://127.0.0.1:8090")
    }

    @Test func restoreFindsTheStoredSession() async throws {
        let model = try await signedInModel()
        #expect(model.state == .signedIn)
        let signedOut = try await signedInModel(store: InMemoryTokenStore())
        #expect(signedOut.state == .signedOut)
    }

    // MARK: - Sign in

    @Test func signInStoresTokens() async throws {
        let store = InMemoryTokenStore()
        let web = FakeWeb.approving()
        let model = try await signedInModel(servers: [server], store: store, web: web)
        await model.appState.load()
        #expect(model.state == .signedOut)

        await model.signIn()

        #expect(model.state == .signedIn)
        #expect(try store.load()?.accessToken == "at-1")
        #expect(try store.load()?.refreshToken == "rt-1")
        let page = try #require(web.opened.first)
        #expect(page.scheme == "wristcall")
        let query = Dictionary(
            (URLComponents(url: page.url, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
        ) { first, _ in first }
        #expect(query["client_id"] == "wristcall-ios")
        #expect(query["redirect_uri"] == "wristcall://auth/callback")
        #expect(query["code_challenge_method"] == "S256")
        #expect(query["scope"] == "openid profile offline_access")
        let exchange = try #require(world.tokenRequests.first).form()
        #expect(exchange["grant_type"] == "authorization_code")
        #expect(exchange["code"] == "code-1")
        #expect(exchange["redirect_uri"] == "wristcall://auth/callback")
        #expect(exchange["code_verifier"]?.isEmpty == false)
        // Signing in mirrors the servers to the agenda (in the background).
        await model.agenda?.idle()
        #expect(world.entries.map(\.url) == ["https://srv.test"])
        #expect(model.appState.servers.first?.cloudServerID == "cs-1")
    }

    @Test func signInCancelledStaysSignedOut() async {
        let appState = AppState(store: InMemoryManagedServerStore())
        let session = session(InMemoryTokenStore())
        let model = AccountModel(cloudURL: cloud.url, session: session, web: FakeWeb(error: CancellationError()), state: appState)
        await model.restore()
        await model.signIn()
        #expect(model.state == .signedOut)
        #expect(world.tokenRequests.isEmpty)
    }

    @Test func signInWithAForgedCallbackFails() async throws {
        let store = InMemoryTokenStore()
        let web = FakeWeb { _ in URL(string: "wristcall://auth/callback?code=stolen&state=other")! }
        let model = try await signedInModel(store: store, web: web)
        await model.signIn()
        guard case .failed(let message) = model.state else {
            Issue.record("expected .failed, got \(model.state)")
            return
        }
        #expect(!message.contains("stolen"))
        #expect(world.tokenRequests.isEmpty)
        #expect(try store.load() == nil)
    }

    // MARK: - Sign out and delete

    @Test func signOutRevokesAndEndsTheBrowserSession() async throws {
        let store = InMemoryTokenStore(fresh)
        let web = FakeWeb.approving()
        var linked = server
        linked.linked = true
        linked.cloudServerID = "cs-9"
        let model = try await signedInModel(servers: [linked], store: store, web: web)
        await model.appState.load()

        await model.signOut()

        #expect(model.state == .signedOut)
        #expect(try store.load() == nil)
        #expect(try world.revokeRequests.first?.form()["token"] == "rt")
        let page = try #require(web.opened.last)
        #expect(page.url.path() == "/oidc/v1/end_session")
        #expect(page.scheme == "wristcall")
        let query = URLComponents(url: page.url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(query.contains { $0.name == "post_logout_redirect_uri" && $0.value == "wristcall://auth/logout" })
        // Servers stay; what belonged to the account (agenda id, link) is forgotten.
        #expect(model.appState.servers.count == 1)
        #expect(model.appState.servers.first?.token == "wc_pat_home")
        #expect(model.appState.servers.first?.cloudServerID == nil)
        #expect(model.appState.servers.first?.linked == false)
    }

    @Test func deleteAccountSignsOut() async throws {
        let store = InMemoryTokenStore(fresh)
        let web = FakeWeb.approving()
        let model = try await signedInModel(servers: [server], store: store, web: web)
        await model.appState.load()
        var hookRan = false
        model.accountDeleted = { hookRan = true }

        await model.deleteAccount()

        let delete = try #require(world.cloudRequests("DELETE", "/v1/account").first)
        #expect(delete.headers["Authorization"] == "Bearer fresh-at")
        #expect(hookRan)
        #expect(model.state == .signedOut)
        #expect(try store.load() == nil)
        #expect(web.opened.last?.url.path() == "/oidc/v1/end_session")
        #expect(model.appState.servers.count == 1)
    }

    @Test func failedDeleteKeepsTheSession() async throws {
        let store = InMemoryTokenStore(fresh)
        let model = try await signedInModel(store: store)
        world.cloudFailure = 503
        var hookRan = false
        model.accountDeleted = { hookRan = true }

        await model.deleteAccount()

        #expect(model.state == .signedIn)
        #expect(model.error != nil)
        #expect(!hookRan)
        #expect(try store.load() != nil)
    }

    @Test func signedOutKeepsServers() async throws {
        // The token endpoint answers invalid_grant to the refresh.
        let fake = FakeServerAPI(health: ownHealth)
        let model = try await signedInModel(api: fake, servers: [server], expiredAccessToken: true)
        await model.appState.load()
        await #expect(throws: AccountError.signedOut) {
            try await model.link(server)
        }
        #expect(model.state == .signedOut)
        #expect(model.appState.servers.count == 1)
        #expect(fake.linkRequests.isEmpty)
    }

    // MARK: - Link

    @Test func linkRefusesForeignIssuer() async throws {
        let fake = FakeServerAPI(health: ServerHealth(version: "0.6.0", relay: nil, account: .init(issuer: "https://evil.test", deviceCredential: "approval")))
        let model = try await signedInModel(api: fake)
        await #expect(throws: AccountError.foreignIssuer("https://evil.test")) {
            try await model.addLinkedServer(urlText: "https://srv.test", code: "12345678", name: nil)
        }
        #expect(!cloudHost.requests.contains { $0.path == "/v1/server-tokens" })
        #expect(fake.linkRequests.isEmpty)
        #expect(model.appState.servers.isEmpty)
        #expect(AccountModel.message(for: AccountError.foreignIssuer("https://evil.test")) == "This server uses another account service.")
    }

    @Test func linkRefusesServerWithoutAccount() async throws {
        let fake = FakeServerAPI(health: ServerHealth(version: "0.6.0"))
        let model = try await signedInModel(api: fake, servers: [server])
        await model.appState.load()
        await #expect(throws: AccountError.serverWithoutAccount) { try await model.link(server) }
        #expect(!cloudHost.requests.contains { $0.path == "/v1/server-tokens" })
        #expect(fake.linkRequests.isEmpty)
    }

    @Test func addLinkedServerSavesApiToken() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        fake.linkResult = .success(AccountLink(linked: true, issuer: world.cloud.url.absoluteString,
                                               user: .init(id: "u1", handle: "gustavo"), apiToken: "wc_pat_new"))
        let model = try await signedInModel(api: fake)
        await model.appState.load()

        let saved = try await model.addLinkedServer(urlText: " https://SRV.test/ ", code: "1234 5678", name: "Home")

        #expect(saved.token == "wc_pat_new")
        #expect(saved.linked)
        #expect(saved.name == "Home")
        #expect(model.appState.servers.map(\.id) == [saved.id])
        #expect(model.appState.servers.first?.token == "wc_pat_new")
        #expect(model.appState.servers.first?.linked == true)
        #expect(fake.linkRequests.count == 1)
        #expect(fake.linkRequests.first?.serverToken == "per-server")
        #expect(fake.linkRequests.first?.code == "12345678")
        let tokenRequest = try #require(world.cloudRequests("POST", "/v1/server-tokens").first)
        #expect(try tokenRequest.json()["audience"] as? String == "https://srv.test")
        // The agenda gets the server, linked.
        await model.agenda?.idle()
        #expect(model.appState.servers.first?.cloudServerID == "cs-1")
        #expect(world.entries.map(\.linked) == [true])
    }

    @Test func addLinkedServerRefusesABadCodeBeforeAnyRequest() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        let model = try await signedInModel(api: fake)
        await #expect(throws: LinkError.badCode) {
            try await model.addLinkedServer(urlText: "https://srv.test", code: "1234567", name: nil)
        }
        await #expect(throws: AddServerError.invalidURL) {
            try await model.addLinkedServer(urlText: "http://srv.test", code: "12345678", name: nil)
        }
        #expect(fake.calls.isEmpty)
        #expect(!cloudHost.requests.contains { $0.path == "/v1/server-tokens" })
    }

    @Test func linkMarksTheServerLinked() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        fake.linkResult = .success(AccountLink(linked: true, issuer: world.cloud.url.absoluteString))
        let model = try await signedInModel(api: fake, servers: [server])
        await model.appState.load()

        try await model.link(server)

        #expect(model.appState.servers.first?.linked == true)
        #expect(fake.linkRequests.first?.serverToken == "per-server")
        #expect(fake.linkRequests.first?.code == nil)
        await model.agenda?.idle()
        #expect(world.entries.first?.linked == true)
    }

    @Test func codeLinkNotSavedSaysHowToRevoke() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        fake.linkResult = .success(AccountLink(linked: true, issuer: world.cloud.url.absoluteString, apiToken: "wc_pat_orphan"))
        fake.verifyError = APIError.unauthorized
        let model = try await signedInModel(api: fake)
        await model.appState.load()

        await #expect(throws: LinkError.notSaved) {
            try await model.addLinkedServer(urlText: "https://srv.test", code: "12345678", name: nil)
        }

        #expect(model.appState.servers.isEmpty)
        let text = AccountModel.message(for: LinkError.notSaved)
        #expect(text.contains("wristcall users tokens revoke"))
        #expect(text.contains("account link"))
        #expect(!text.contains("wc_pat_orphan"))
        #expect(!text.contains("12345678"))
        #expect(!text.contains("`"))
    }

    @Test func linkConflictIsAnotherUser() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        fake.linkResult = .failure(APIError.conflict(code: "conflict", message: "this central account is already linked to another user"))
        let model = try await signedInModel(api: fake, servers: [server])
        await model.appState.load()

        await #expect(throws: APIError.self) { try await model.link(server) }

        // The server's 409 means another local user has this account: never counted as linked.
        #expect(model.appState.servers.first?.linked == false)
        #expect(AccountModel.message(for: APIError.conflict(code: "conflict", message: "x")) ==
            "Already linked to another user on this server.")
    }

    @Test func linkNeverSendsAccessToken() async throws {
        let cloudURL = world.cloud.url.absoluteString
        let host = StubHost { request in
            switch request.path {
            case "/v1/health":
                return .init(200, #"{"status":"ok","version":"0.6.0","protocol":1,"account":{"issuer":"\#(cloudURL)","device_credential":"approval"},"push":null}"#)
            case "/v1/account/link":
                return .init(200, #"{"linked":true,"issuer":"\#(cloudURL)","user":{"id":"u1","handle":"g"},"api_token":"wc_pat_new"}"#)
            default:
                return .init(200, #"{"providers":[],"custom_endpoints":false}"#)
            }
        }
        let appState = AppState(store: InMemoryManagedServerStore(), makeAPI: { LiveServerAPI(server: $0, token: $1, session: .stubbed()) })
        let model = AccountModel(cloudURL: world.cloud.url, session: session(InMemoryTokenStore(fresh)), web: FakeWeb.approving(), state: appState)
        await model.restore()

        let saved = try await model.addLinkedServer(urlText: host.url.absoluteString, code: "12345678", name: nil)
        try await model.link(saved)

        let links = host.requests.filter { $0.path == "/v1/account/link" }
        #expect(links.count == 2)
        for request in host.requests {
            let body = request.body.map { String(decoding: $0, as: UTF8.self) } ?? ""
            #expect(!body.contains("fresh-at"))
            #expect(!request.headers.values.contains { $0.contains("fresh-at") })
        }
        #expect(try links[0].json()["token"] as? String == "per-server")
        #expect(try links[0].json()["code"] as? String == "12345678")
        #expect(!links[0].headers.keys.contains { $0.lowercased() == "authorization" })
        #expect(try links[1].json()["token"] as? String == "per-server")
        #expect(links[1].headers["Authorization"] == "Bearer wc_pat_new")
    }

    @Test func deleteExplanationSaysWhatStays() {
        #expect(AccountSection.deleteExplanation == "Deletes your server list from wristcall Cloud. Your servers and "
            + "their data stay as they are. Your sign-in stays until you delete it on the account page.")
    }

    @Test func messagesNeverCarrySecrets() {
        let errors: [any Error] = [
            AccountError.signedOut, AccountError.notConfigured, AccountError.serverWithoutAccount,
            APIError.invalidCode, APIError.network(.notConnectedToInternet), LinkError.badCode,
            OIDCError.stateMismatch, CancellationError(),
        ]
        for error in errors {
            let text = AccountModel.message(for: error)
            #expect(!text.isEmpty)
            #expect(!text.contains("wc_pat_"))
        }
    }
}
