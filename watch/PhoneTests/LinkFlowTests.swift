import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

/// wristcall links on the iPhone (spec O5): the router, linking a server by its QR code (Cloud and server
/// stubbed) and approving a web sign-in.
@MainActor
struct LinkFlowTests {
    let world = AccountWorld()
    static let code = LinkCode("ABCDEFGHIJKLMNOPQRSTUVWXYZ")!
    static let webID = "ws_0123456789abcdef"
    static let webKey = "approve-key-Secret_123"

    /// A silent account `acc_1` with its key, signed in here.
    func readyRig(
        servers: [ManagedServer] = [],
        makeAPI: @escaping ServerAPIFactory = { LiveServerAPI(server: $0, token: $1, session: .stubbed()) }
    ) async -> AccountRig {
        let key = StoredAccountKey.of("acc_1", age: 60)
        world.addAccount("acc_1", key: key.key, AccountWorld.Account())
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: [key], servers: servers, makeAPI: makeAPI)
        _ = await rig.started()
        return rig
    }

    /// A wristcall server whose account is this test's Cloud. `linkReplies` answer `POST /v1/account/link`
    /// in turn (the last one repeats).
    func serverHost(health: String? = nil, linkReplies: [StubHost.Reply]? = nil) -> StubHost {
        let cloud = world.cloud.url.absoluteString
        let health = health
            ?? #"{"status":"ok","version":"0.8.0","protocol":1,"account":{"issuer":"\#(cloud)","device_credential":"approval"},"push":null}"#
        let replies = linkReplies ?? [StubHost.Reply(200, #"{"linked":true,"issuer":"\#(cloud)","user":{"id":"u1","handle":"g"},"api_token":"wc_pat_new"}"#)]
        let links = Mutex(0)
        return StubHost { request in
            switch (request.method, request.path) {
            case ("GET", "/v1/health"):
                return .init(200, health)
            case ("POST", "/v1/account/link"):
                let index = links.withLock { value in
                    defer { value += 1 }
                    return value
                }
                return replies[min(index, replies.count - 1)]
            case ("POST", "/v1/pairing-codes"):
                let expires = Date().timeIntervalSince1970 + 600
                return .init(201, #"{"code":"12345678","expires_at":\#(expires),"server_url":"https://x","via_directory":false}"#)
            default:
                return .init(200, #"{"providers":[],"custom_endpoints":false}"#)
            }
        }
    }

    func link(to url: URL) -> URL {
        let server = url.absoluteString.addingPercentEncoding(withAllowedCharacters: .alphanumerics)!
        return URL(string: "wristcall://link#v=1&s=\(server)&c=\(Self.code.value)")!
    }

    // MARK: - Router

    @Test func routerIgnoresAuthCallbacks() {
        let router = LinkRouter()
        #expect(router.open(URL(string: "wristcall://auth/callback?code=x")!) == false)
        #expect(router.open(URL(string: "wristcall://auth/logout")!) == false)
        #expect(router.sheet == nil)
    }

    @Test func routerOpensServerAndWebLinks() {
        let router = LinkRouter()
        let server = URL(string: "https://home.example.com")!

        #expect(router.open(URL(string: "https://wristcall-app.trigram.com.br/link#v=1&s=https%3A%2F%2Fhome.example.com&c=\(Self.code.value)")!))
        #expect(router.sheet == .server(server, Self.code))

        router.sheet = nil
        #expect(router.open(URL(string: "wristcall://link#v=1&s=https%3A%2F%2Fhome.example.com&c=\(Self.code.value)")!))
        #expect(router.sheet == .server(server, Self.code))

        #expect(router.open(URL(string: "wristcall://approve#w=\(Self.webID)&k=\(Self.webKey)")!))
        #expect(router.sheet == .web(id: Self.webID, key: Self.webKey))

        #expect(router.open(URL(string: "https://wristcall-app.trigram.com.br/approve#w=\(Self.webID)&k=\(Self.webKey)")!))
        #expect(router.sheet == .web(id: Self.webID, key: Self.webKey))

        // A bad fragment (short code, wrong version, http server), another wristcall link: invalid.
        for bad in [
            "wristcall://link#v=1&s=https%3A%2F%2Fhome.example.com&c=ABC",
            "wristcall://link#v=2&s=https%3A%2F%2Fhome.example.com&c=\(Self.code.value)",
            "https://wristcall-app.trigram.com.br/link#v=1&s=http%3A%2F%2Fhome.example.com&c=\(Self.code.value)",
            "https://wristcall-app.trigram.com.br/approve#w=nope&k=x",
            "wristcall://something",
        ] {
            router.sheet = nil
            #expect(router.open(URL(string: bad)!), "\(bad)")
            #expect(router.sheet == .invalid, "\(bad)")
        }

        // Not ours: nothing changes.
        router.sheet = nil
        #expect(router.open(URL(string: "https://example.com/link#v=1")!) == false)
        #expect(router.sheet == nil)
        #expect(LinkRouter.invalidMessage == "This link isn't valid or has expired. Get a new one with `wristcall link` on your server.")
    }

    @Test func sheetDescriptionHidesSecrets() {
        let server = LinkRouter.Sheet.server(URL(string: "https://home.example.com")!, Self.code)
        let web = LinkRouter.Sheet.web(id: Self.webID, key: Self.webKey)
        for sheet in [server, web] {
            for text in [String(describing: sheet), String(reflecting: sheet), "\(sheet)", String(describing: Mirror(reflecting: sheet).children.map(\.value))] {
                #expect(!text.contains(Self.code.value))
                #expect(!text.contains(Self.webKey))
            }
        }
        #expect(String(describing: server).contains("home.example.com"))
    }

    // MARK: - Server link

    @Test func linkStoresTheServerMirrorsAndPairsTheWatch() async throws {
        let host = serverHost()
        let rig = await readyRig()
        let model = rig.model
        let watch = FakeWatchSession()
        model.watchLink = WatchLink(session: watch)

        let saved = try await model.addLinkedServer(host.url, code: Self.code)
        await model.agenda?.idle()
        _ = await model.watchPairing?.value

        // The per-server token names the server's canonical URL.
        let tokens = world.cloudRequests("POST", "/v1/server-tokens")
        #expect(tokens.count == 1)
        #expect(try tokens.first?.json()["audience"] as? String == ServerAddress.canonical(host.url))
        // The link carries the code and the per-server token, never an `Authorization`.
        let links = host.requests.filter { $0.path == "/v1/account/link" }
        #expect(links.count == 1)
        #expect(try links[0].json()["code"] as? String == Self.code.value)
        #expect(try links[0].json()["token"] as? String == "per-server")
        #expect(!links[0].headers.keys.contains { $0.lowercased() == "authorization" })
        // Saved with the server's personal token, linked, mirrored.
        #expect(saved.token == "wc_pat_new")
        #expect(saved.linked)
        #expect(model.appState.servers.map(\.id) == [saved.id])
        let posted = world.cloudRequests("POST", "/v1/servers")
        #expect(posted.count == 1)
        #expect(try posted.first?.json()["linked"] as? Bool == true)
        #expect(world.entries.first?.linked == true)
        // The watch got a pairing code for it.
        #expect(watch.sent.count == 1)
        #expect(watch.sent.first?["type"] as? String == "pair")
        #expect(watch.sent.first?["server_url"] as? String == host.url.absoluteString)
    }

    @Test func secondScanOfTheSameCodeDoesNotDuplicate() async throws {
        let cloud = world.cloud.url.absoluteString
        let host = serverHost(linkReplies: [
            .init(200, #"{"linked":true,"issuer":"\#(cloud)","api_token":"wc_pat_new"}"#),
            .init(401, #"{"error":"invalid_code","message":"unknown, used or expired code"}"#),
        ])
        let model = await readyRig().model

        try await model.addLinkedServer(host.url, code: Self.code)
        await #expect(throws: APIError.invalidCode) {
            try await model.addLinkedServer(host.url, code: Self.code)
        }
        #expect(model.appState.servers.count == 1)
        #expect(AccountModel.linkMessage(for: APIError.invalidCode)
            == "This code was already used or has expired. Run `wristcall link` on the server for a new one.")

        // The confirmation for the same server now says it is connected already.
        let confirm = ServerLinkModel(server: host.url, code: Self.code, account: model)
        #expect(confirm.phase == .alreadyConnected)
        #expect(confirm.subtitle == "Already connected")
        #expect(!confirm.canConnect)
    }

    @Test func linkErrorsHaveTheirText() {
        #expect(AccountModel.linkMessage(for: APIError.conflict(code: "conflict", message: "x"))
            == "Your account is already linked to another user on this server.")
        #expect(AccountModel.linkMessage(for: APIError.invalidAccountToken) == "The server didn't accept the sign-in. Try again.")
        #expect(AccountModel.linkMessage(for: APIError.rateLimited) == "Too many attempts. Try again in a minute.")
        #expect(AccountModel.linkMessage(for: APIError.retryLater(30)) == "Too many attempts. Try again in a minute.")
        #expect(AccountModel.linkMessage(for: AccountError.foreignIssuer("https://evil.test"))
            == "This server uses another wristcall account service.")
        #expect(AccountModel.linkMessage(for: PairingError.network(.cannotFindHost)) == "Can't reach the server.")
        #expect(AccountModel.linkMessage(for: PairingError.notFound) == "This doesn't look like a wristcall server.")
    }

    @Test func confirmationShowsTheASCIIHost() async throws {
        let model = await readyRig().model
        let router = LinkRouter()
        router.open(URL(string: "wristcall://link#v=1&s=https%3A%2F%2Fxn--caf-dma.example&c=\(Self.code.value)")!)
        guard case .server(let server, let code) = router.sheet else {
            Issue.record("no server sheet")
            return
        }
        let confirm = ServerLinkModel(server: server, code: code, account: model)
        #expect(confirm.title == "Connect to xn--caf-dma.example?")
        #expect(confirm.phase == .confirm)
    }

    @Test func serverWithoutAccountIsRefusedBeforeAnyCloudCall() async throws {
        let host = serverHost(health: #"{"status":"ok","version":"0.8.0","protocol":1,"account":null,"push":null}"#)
        let model = await readyRig().model

        await #expect(throws: AccountError.serverWithoutAccount) {
            try await model.addLinkedServer(host.url, code: Self.code)
        }
        #expect(world.cloudRequests("POST", "/v1/server-tokens").isEmpty)
        #expect(!host.requests.contains { $0.path == "/v1/account/link" })
        #expect(model.appState.servers.isEmpty)

        // The confirmation refuses it on its health already.
        let confirm = ServerLinkModel(server: host.url, code: Self.code, account: model)
        await confirm.load()
        #expect(confirm.error == "This server isn't set up for wristcall accounts.")
        #expect(confirm.subtitle == "wristcall server 0.8.0")
        #expect(!confirm.canConnect)
    }

    @Test func confirmationWaitsForTheAccount() async throws {
        let rig = AccountRig(world)
        let model = rig.model
        let confirm = ServerLinkModel(server: URL(string: "https://home.example.com")!, code: Self.code, account: model)
        #expect(model.state == .starting)
        #expect(confirm.accountNote == "Setting up your account…")
        #expect(!confirm.canConnect)
        await #expect(throws: LinkError.accountNotReady) {
            try await model.addLinkedServer(URL(string: "https://home.example.com")!, code: Self.code)
        }
    }

    @Test func connectLinksAndShowsErrorsInPlace() async throws {
        let host = serverHost(linkReplies: [.init(409, #"{"error":"conflict","message":"linked to another user"}"#)])
        let model = await readyRig().model
        let confirm = ServerLinkModel(server: host.url, code: Self.code, account: model)
        await confirm.load()
        #expect(confirm.canConnect)

        #expect(await confirm.connect() == false)
        #expect(confirm.error == "Your account is already linked to another user on this server.")
        #expect(confirm.phase == .confirm)
        #expect(model.appState.servers.isEmpty)
    }

    // MARK: - Web sign-in

    @Test func webApprovalInspectsThenApproves() async throws {
        let model = await readyRig().model
        let approval = WebApprovalModel(id: Self.webID, key: Self.webKey, account: model)

        await approval.load()
        let info = try #require(approval.info)
        #expect(info.name == "Firefox on Mac")
        #expect(WebApprovalModel.pageText(info.name) == "Page says: “Firefox on Mac”")
        #expect(WebApprovalModel.countryText(info.country, locale: Locale(identifier: "en_US")) == "Brazil")

        #expect(await approval.approve())
        #expect(approval.phase == .approved)
        let inspect = world.cloudRequests("POST", "/v1/web-sessions/\(Self.webID)/inspect")
        let approve = world.cloudRequests("POST", "/v1/web-sessions/\(Self.webID)/approve")
        #expect(inspect.count == 1)
        #expect(approve.count == 1)
        #expect(try approve.first?.json()["approve_key"] as? String == Self.webKey)
        #expect(approve.first?.headers["Authorization"]?.hasPrefix("Bearer at-acc_1") == true)
    }

    @Test func expiredWebCodeShowsReload() async throws {
        world.webInspectReply = .init(404, #"{"error":"not_found","message":"web session not found"}"#)
        let model = await readyRig().model
        let approval = WebApprovalModel(id: Self.webID, key: Self.webKey, account: model)

        await approval.load()
        #expect(approval.phase == .failed("This code expired. Reload the page for a new one."))
        #expect(world.cloudRequests("POST", "/v1/web-sessions/\(Self.webID)/approve").isEmpty)
    }

    @Test func webApprovalLimitShowsDevices() async throws {
        world.webApproveReply = .init(403, #"{"error":"limit","message":"too many web sessions"}"#)
        let model = await readyRig().model
        let approval = WebApprovalModel(id: Self.webID, key: Self.webKey, account: model)

        await approval.load()
        #expect(await approval.approve() == false)
        #expect(approval.phase == .failed("Too many web sessions are open. Sign out of one in Devices first."))
    }

    @Test func webTextsStayOnOneLine() {
        #expect(WebApprovalModel.pageText("Evil\nApprove me") == "Page says: “Evil Approve me”")
        #expect(WebApprovalModel.browserText(nil) == "Unknown browser")
        #expect(WebApprovalModel.browserText("Mozilla/5.0\r\n(X)") == "Mozilla/5.0 (X)")
        #expect(WebApprovalModel.browserText(" \n ") == "Unknown browser")
        #expect(WebApprovalModel.countryText(nil) == "Unknown location")
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        #expect(WebApprovalModel.openedText(now.addingTimeInterval(-120), now: now, locale: Locale(identifier: "en_US")) == "Opened 2 minutes ago")
    }
}
