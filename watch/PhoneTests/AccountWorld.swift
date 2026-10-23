import Foundation
import Synchronization
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

/// A Cloud with its agenda, its accounts and device sessions, its issuer (discovery) and the issuer's
/// endpoints (token, revocation, end session), all stubbed. The agenda keeps servers in memory like the real
/// one: ids `cs-1`, `cs-2`…, one entry per URL (`409` on a second), `404` for unknown ids.
///
/// Accounts: `acc_1`, `acc_2`… made by `POST /v1/accounts`, and `acc_z` the 0.3.x account of the Zitadel
/// user. Sessions: device `dev_<n>`, access token `at-<account>-<n>`, refresh token `rt-<account>-<n>`, an
/// hour after `clock`. A key opens the account it was registered for (`addAccount(key:)`, sign-up or `PUT`).
final class AccountWorld: Sendable {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_800_000_000)
    static let zitadelAccount = "acc_z"

    let endpoints: StubHost
    let issuer: StubHost
    let cloud: StubHost

    struct Entry: Sendable {
        var id: String
        var name: String
        var url: String
        /// `self-hosted`, or `cloud` for the Wristcall Cloud's own entry.
        var kind = "self-hosted"
        var linked: Bool
        var agents: [[String: String]] = []
    }

    struct Account: Sendable {
        var servers = 0
        /// The plan's JSON (`{"plan":"full","source":"invite"}`), or `nil`.
        var plan: String?
        var identities: [String] = []
        var keySet = true
    }

    private struct State {
        var accounts: [String: Account] = [:]
        /// Key value → the account it opens.
        var keyOwners: [String: String] = [:]
        var nextAccount = 1
        var nextDevice = 1
        /// Replies `POST /v1/accounts` gives first, one per call, before it makes accounts.
        var signUpReplies: [StubHost.Reply] = []
        /// `POST /v1/devices` with a Zitadel token answers this status when set.
        var zitadelSignInFailure: Int?
        var refuseSessionRefresh = false
        var enrollLimit = false
        /// Seconds an enroll code lasts.
        var enrollTTL = 600.0
        /// `GET /v1/devices` answers this JSON.
        var devicesJSON = #"{"devices":[]}"#
        /// `GET /v1/account` answers this status when set.
        var accountReadFailure: Int?
        var entries: [Entry] = []
        var nextID = 1
        var refuseRefresh = false
        var cloudFailure: Int?
        /// Agenda writes (anything but GET) wait while this is set: a hung Cloud.
        var holding = false
        /// Writes waiting in the hold right now.
        var waiting = 0
        /// What `POST /v1/web-sessions/{id}/inspect` and `/approve` answer (bearer checked first).
        var webInspectReply = StubHost.Reply(
            200, #"{"name":"Firefox on Mac","user_agent":"Mozilla/5.0 (Macintosh)","country":"BR","created_at":1800000000,"expires_at":1800000300}"#)
        var webApproveReply = StubHost.Reply(200, #"{"ok":true}"#)
    }

    private final class TextBox: Sendable {
        let value = Mutex("")
    }

    private final class Box: Sendable {
        let state = Mutex(State())
    }

    private let box = Box()

    init() {
        let box = box
        endpoints = StubHost { request in
            if request.path.hasSuffix("/revoke") { return StubHost.Reply(200, "") }
            let form = try request.form()
            if form["grant_type"] == "refresh_token", box.state.withLock({ $0.refuseRefresh }) {
                return StubHost.Reply(400, #"{"error":"invalid_grant"}"#)
            }
            return StubHost.Reply(200, #"{"access_token":"at-1","refresh_token":"rt-1","id_token":"idt-1","expires_in":3600,"token_type":"Bearer"}"#)
        }
        let endpointsBase = endpoints.url.absoluteString
        let issuerURL = TextBox()
        let issuer = StubHost { _ in
            let url = issuerURL.value.withLock { $0 }
            return StubHost.Reply(200, """
                {"issuer":"\(url)",
                 "authorization_endpoint":"\(endpointsBase)/oauth/v2/authorize",
                 "token_endpoint":"\(endpointsBase)/oauth/v2/token",
                 "revocation_endpoint":"\(endpointsBase)/oauth/v2/revoke",
                 "end_session_endpoint":"\(endpointsBase)/oidc/v1/end_session",
                 "code_challenge_methods_supported":["S256"]}
                """)
        }
        issuerURL.value.withLock { $0 = issuer.url.absoluteString }
        self.issuer = issuer
        let issuerText = issuer.url.absoluteString
        cloud = StubHost { request in try AccountWorld.agenda(request, box: box, issuer: issuerText) }
    }

    // MARK: - Accounts

    /// Adds an account (opened by `key`, when given) to the Cloud.
    func addAccount(_ id: String, key: AccountKey? = nil, _ account: Account = Account()) {
        box.state.withLock { state in
            state.accounts[id] = account
            if let key { state.keyOwners[key.value] = id }
        }
    }

    func account(_ id: String) -> Account? { box.state.withLock { $0.accounts[id] } }
    var accountIDs: Set<String> { box.state.withLock { Set($0.accounts.keys) } }

    /// The account a key opens now (`nil`: none, or its account was deleted).
    func owner(of key: AccountKey) -> String? { box.state.withLock { $0.keyOwners[key.value] } }

    /// A stored session of `account` (made by `POST /v1/devices` earlier), fresh for an hour or already expired.
    func storedSession(_ account: String, expired: Bool = false) -> StoredDeviceSession {
        StoredDeviceSession(
            account: account, device: "dev_0", refreshToken: "rt-\(account)-0", accessToken: "at-\(account)-0",
            expiresAt: Self.clock.addingTimeInterval(expired ? 10 : 3600)
        )
    }

    /// What `POST /v1/accounts` answers first (then it makes accounts again).
    var signUpReplies: [StubHost.Reply] {
        get { box.state.withLock { $0.signUpReplies } }
        set { box.state.withLock { $0.signUpReplies = newValue } }
    }

    var zitadelSignInFailure: Int? {
        get { box.state.withLock { $0.zitadelSignInFailure } }
        set { box.state.withLock { $0.zitadelSignInFailure = newValue } }
    }

    /// Session refreshes get `401 revoked` (the device was signed out elsewhere).
    var refuseSessionRefresh: Bool {
        get { box.state.withLock { $0.refuseSessionRefresh } }
        set { box.state.withLock { $0.refuseSessionRefresh = newValue } }
    }

    var accountReadFailure: Int? {
        get { box.state.withLock { $0.accountReadFailure } }
        set { box.state.withLock { $0.accountReadFailure = newValue } }
    }

    /// Enroll codes get `403 limit`.
    var enrollLimit: Bool {
        get { box.state.withLock { $0.enrollLimit } }
        set { box.state.withLock { $0.enrollLimit = newValue } }
    }

    var enrollTTL: Double {
        get { box.state.withLock { $0.enrollTTL } }
        set { box.state.withLock { $0.enrollTTL = newValue } }
    }

    /// What `GET /v1/devices` answers.
    var devicesJSON: String {
        get { box.state.withLock { $0.devicesJSON } }
        set { box.state.withLock { $0.devicesJSON = newValue } }
    }

    /// Zitadel refresh requests get `invalid_grant` (the refresh token was revoked).
    var refuseRefresh: Bool {
        get { box.state.withLock { $0.refuseRefresh } }
        set { box.state.withLock { $0.refuseRefresh = newValue } }
    }

    /// Every agenda route (all but `/v1/config`) answers this status when set.
    var cloudFailure: Int? {
        get { box.state.withLock { $0.cloudFailure } }
        set { box.state.withLock { $0.cloudFailure = newValue } }
    }

    var entries: [Entry] { box.state.withLock { $0.entries } }

    var webInspectReply: StubHost.Reply {
        get { box.state.withLock { $0.webInspectReply } }
        set { box.state.withLock { $0.webInspectReply = newValue } }
    }

    var webApproveReply: StubHost.Reply {
        get { box.state.withLock { $0.webApproveReply } }
        set { box.state.withLock { $0.webApproveReply = newValue } }
    }

    /// Holds every agenda write until `release()` (at most 10 s, so a failing test cannot hang). The stub
    /// answers on URLSession's protocol thread, so other stubbed hosts wait too while a write is held.
    func hold() { box.state.withLock { $0.holding = true } }
    func release() { box.state.withLock { $0.holding = false } }
    var waiting: Int { box.state.withLock { $0.waiting } }

    @discardableResult
    func seed(name: String, url: String, kind: String = "self-hosted", linked: Bool = false,
              agents: [[String: String]] = []) -> String {
        box.state.withLock { state in
            let id = "cs-\(state.nextID)"
            state.entries.append(Entry(id: id, name: name, url: url, kind: kind, linked: linked, agents: agents))
            state.nextID += 1
            return id
        }
    }

    /// The Cloud's own entry (`kind: cloud`, linked), the way the Cloud adds it when the account has a plan.
    @discardableResult
    func seedCloudEntry(agents: [[String: String]] = []) -> String {
        seed(name: "Wristcall Cloud", url: "https://cloud.wristcall.test", kind: "cloud", linked: true, agents: agents)
    }

    /// Takes an entry out of the agenda behind the apps' back (the Cloud dropped it).
    func dropEntry(_ id: String) {
        box.state.withLock { $0.entries.removeAll { $0.id == id } }
    }

    /// `POST /v1/route` answers this node (token `node-tok`, five minutes after `clock`).
    static let routeNode = URL(string: "https://node-a.example")!

    var tokenRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/token") } }
    /// The Cloud requests sent with a token (`Authorization`), anything else than a device session's.
    var foreignTokenRequests: [StubHost.Request] {
        cloud.requests.filter { request in
            guard let header = request.headers["Authorization"] else { return false }
            return !header.hasPrefix("Bearer at-acc_")
        }
    }
    var revokeRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/revoke") } }
    func cloudRequests(_ method: String, _ path: String) -> [StubHost.Request] {
        cloud.requests.filter { $0.method == method && $0.path == path }
    }

    private static func agenda(_ request: StubHost.Request, box: Box, issuer: String) throws -> StubHost.Reply {
        if request.path == "/v1/config" {
            return StubHost.Reply(200, """
                {"issuer":"\(issuer)","project_id":"345678901234567890",
                 "clients":{"ios":"wristcall-ios","pwa":"wristcall-pwa","watch":"wristcall-watch"},
                 "scopes":["openid","profile","offline_access"],
                 "server_tokens":true,
                 "push":{"apns":false,"webpush":false,"vapid_public_key":null,"apns_topics":[]}}
                """)
        }
        if request.method != "GET", box.state.withLock({ $0.holding }) {
            box.state.withLock { $0.waiting += 1 }
            let deadline = Date().addingTimeInterval(10)
            while box.state.withLock({ $0.holding }), Date() < deadline {
                Thread.sleep(forTimeInterval: 0.005)
            }
            box.state.withLock { $0.waiting -= 1 }
        }
        if let status = box.state.withLock({ $0.cloudFailure }) {
            return StubHost.Reply(status, #"{"error":"unavailable","message":"down"}"#)
        }
        if let reply = try sessions(request, box: box) { return reply }
        let parts = request.path.split(separator: "/").map(String.init)
        let route = parts.count >= 3 && parts[0] == "v1" && parts[1] == "servers"
            ? (parts.count == 4 && parts[3] == "agents" ? "servers/id/agents" : (parts.count == 3 ? "servers/id" : "?"))
            : parts.joined(separator: "/")
        let id = parts.count >= 3 ? parts[2] : ""
        switch (request.method, route) {
        case ("POST", "v1/route"):
            guard bearerAccount(request) != nil else { return revoked }
            return StubHost.Reply(200, #"{"node":{"id":"node-a","url":"\#(routeNode.absoluteString)","region":"gru"},"token":"node-tok","expires_at":1800000300,"fallback":null}"#)
        case ("POST", "v1/server-tokens"):
            let audience = try request.json()["audience"] as? String ?? ""
            return StubHost.Reply(200, #"{"token":"per-server","audience":"\#(audience)","expires_at":1800000300}"#)
        case ("DELETE", "v1/account"):
            guard let account = Self.bearerAccount(request) else { return Self.revoked }
            return box.state.withLock { state in
                guard state.accounts[account] != nil else { return Self.revoked }
                state.accounts[account] = nil
                state.keyOwners = state.keyOwners.filter { $0.value != account }
                state.entries = []
                return StubHost.Reply(204, "")
            }
        case ("GET", "v1/servers"):
            let list = box.state.withLock { $0.entries }.map(Self.json)
            return StubHost.Reply(200, #"{"servers":[\#(list.joined(separator: ","))]}"#)
        case ("POST", "v1/servers"):
            let body = try request.json()
            let url = body["url"] as? String ?? ""
            return box.state.withLock { state in
                if state.entries.contains(where: { $0.url == url }) {
                    return StubHost.Reply(409, #"{"error":"conflict","message":"this server is already in the agenda"}"#)
                }
                let entry = Entry(id: "cs-\(state.nextID)", name: body["name"] as? String ?? "", url: url,
                                  linked: body["linked"] as? Bool ?? false)
                state.nextID += 1
                state.entries.append(entry)
                return StubHost.Reply(201, Self.json(entry))
            }
        case ("PATCH", "servers/id"):
            let body = try request.json()
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                if let name = body["name"] as? String { state.entries[index].name = name }
                if let linked = body["linked"] as? Bool { state.entries[index].linked = linked }
                return StubHost.Reply(200, Self.json(state.entries[index]))
            }
        case ("DELETE", "servers/id"):
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                state.entries.remove(at: index)
                return StubHost.Reply(204, "")
            }
        case ("PUT", "servers/id/agents"):
            let agents = (try request.json()["agents"] as? [[String: String]]) ?? []
            return box.state.withLock { state in
                guard let index = state.entries.firstIndex(where: { $0.id == id }) else {
                    return StubHost.Reply(404, #"{"error":"not_found","message":"server not found"}"#)
                }
                state.entries[index].agents = agents
                return StubHost.Reply(200, Self.json(state.entries[index]))
            }
        default:
            return StubHost.Reply(404, #"{"error":"not_found","message":"no route"}"#)
        }
    }

    // MARK: - Sessions

    private static let revoked = StubHost.Reply(401, #"{"error":"revoked","message":"revoked"}"#)
    private static let unauthorized = StubHost.Reply(401, #"{"error":"unauthorized","message":"no"}"#)

    /// `at-<account>-<n>` → `<account>`.
    private static func bearerAccount(_ request: StubHost.Request) -> String? {
        guard let header = request.headers["Authorization"], header.hasPrefix("Bearer at-") else { return nil }
        let parts = header.dropFirst("Bearer ".count).split(separator: "-")
        return parts.count == 3 ? String(parts[1]) : nil
    }

    private static func session(_ state: inout State, account: String, keySet: Bool?) -> StubHost.Reply {
        let device = state.nextDevice
        state.nextDevice += 1
        let keyField = keySet.map { #","key_set":\#($0)"# } ?? ""
        return StubHost.Reply(201, #"{"account":"\#(account)","device":"dev_\#(device)","access_token":"at-\#(account)-\#(device)","refresh_token":"rt-\#(account)-\#(device)","expires_at":1800003600\#(keyField)}"#)
    }

    /// The account and session routes; `nil` for the agenda's.
    private static func sessions(_ request: StubHost.Request, box: Box) throws -> StubHost.Reply? {
        switch (request.method, request.path) {
        case ("POST", "/v1/accounts"):
            let key = try request.json()["account_key"] as? String ?? ""
            return box.state.withLock { state in
                if !state.signUpReplies.isEmpty { return state.signUpReplies.removeFirst() }
                let account = "acc_\(state.nextAccount)"
                state.nextAccount += 1
                state.accounts[account] = Account()
                state.keyOwners[key] = account
                return session(&state, account: account, keySet: nil)
            }
        case ("POST", "/v1/devices"):
            if request.headers["Authorization"] != nil {
                return box.state.withLock { state in
                    if let status = state.zitadelSignInFailure {
                        return StubHost.Reply(status, #"{"error":"unavailable","message":"down"}"#)
                    }
                    let account = AccountWorld.zitadelAccount
                    var record = state.accounts[account] ?? Account(identities: ["zitadel"], keySet: false)
                    if !record.identities.contains("zitadel") { record.identities.append("zitadel") }
                    state.accounts[account] = record
                    return session(&state, account: account, keySet: record.keySet)
                }
            }
            let body = try request.json()
            let key = body["account_key"] as? String ?? ""
            let named = body["account"] as? String
            return box.state.withLock { state in
                guard let owner = state.keyOwners[key], state.accounts[owner] != nil, named == nil || named == owner
                else { return unauthorized }
                return session(&state, account: owner, keySet: true)
            }
        case ("POST", "/v1/session/refresh"):
            let token = try request.json()["refresh_token"] as? String ?? ""
            let parts = token.split(separator: "-")
            return box.state.withLock { state in
                guard !state.refuseSessionRefresh, parts.count == 3, state.accounts[String(parts[1])] != nil
                else { return revoked }
                var reply = session(&state, account: String(parts[1]), keySet: nil)
                reply.status = 200
                return reply
            }
        case ("GET", "/v1/account"):
            guard let account = bearerAccount(request) else { return revoked }
            return box.state.withLock { state in
                if let status = state.accountReadFailure {
                    return StubHost.Reply(status, #"{"error":"unavailable","message":"down"}"#)
                }
                guard let record = state.accounts[account] else { return revoked }
                let identities = record.identities.map { "\"\($0)\"" }.joined(separator: ",")
                return StubHost.Reply(200, #"{"account":"\#(account)","created_at":1,"servers":\#(record.servers),"plan":\#(record.plan ?? "null"),"identities":[\#(identities)],"key_set":\#(record.keySet)}"#)
            }
        case ("PUT", "/v1/account/key"):
            guard let account = bearerAccount(request) else { return revoked }
            let key = try request.json()["account_key"] as? String ?? ""
            return box.state.withLock { state in
                guard var record = state.accounts[account] else { return revoked }
                if record.keySet, state.keyOwners[key] != account {
                    return StubHost.Reply(409, #"{"error":"key_exists","message":"exists"}"#)
                }
                record.keySet = true
                state.accounts[account] = record
                state.keyOwners[key] = account
                return StubHost.Reply(204, "")
            }
        case ("GET", "/v1/devices"):
            guard bearerAccount(request) != nil else { return revoked }
            return StubHost.Reply(200, box.state.withLock { $0.devicesJSON })
        case ("POST", "/v1/devices/enroll-codes"):
            guard bearerAccount(request) != nil else { return revoked }
            return box.state.withLock { state in
                if state.enrollLimit { return StubHost.Reply(403, #"{"error":"limit","message":"limit"}"#) }
                let number = state.nextDevice
                state.nextDevice += 1
                let expires = Date().timeIntervalSince1970 + state.enrollTTL
                return StubHost.Reply(201, #"{"code":"code-\#(number)","expires_at":\#(expires)}"#)
            }
        case ("POST", "/v1/entitlements/redeem"):
            guard let account = bearerAccount(request) else { return revoked }
            return box.state.withLock { state in
                guard var record = state.accounts[account] else { return revoked }
                if record.plan != nil {
                    return StubHost.Reply(409, #"{"error":"already_entitled","message":"has a plan"}"#)
                }
                record.plan = #"{"plan":"full","source":"invite"}"#
                state.accounts[account] = record
                return StubHost.Reply(200, record.plan!)
            }
        case ("POST", "/v1/account/key/rotate"):
            guard bearerAccount(request) != nil else { return revoked }
            return StubHost.Reply(200, #"{"revoked":2}"#)
        default:
            if request.method == "DELETE", request.path.hasPrefix("/v1/devices/") { return StubHost.Reply(204, "") }
            if request.method == "POST", request.path.hasPrefix("/v1/web-sessions/") {
                guard bearerAccount(request) != nil else { return revoked }
                if request.path.hasSuffix("/inspect") { return box.state.withLock { $0.webInspectReply } }
                if request.path.hasSuffix("/approve") { return box.state.withLock { $0.webApproveReply } }
            }
            return nil
        }
    }

    private static func json(_ entry: Entry) -> String {
        let object: [String: Any] = [
            "id": entry.id, "name": entry.name, "url": entry.url, "kind": entry.kind, "linked": entry.linked,
            "agents": entry.agents, "created_at": 1, "updated_at": 1,
        ]
        let data = try! JSONSerialization.data(withJSONObject: object)
        return String(decoding: data, as: UTF8.self)
    }
}

/// A `WebAuthenticator` that records the pages it was asked to open and answers with `respond`.
@MainActor
final class FakeWeb: WebAuthenticator {
    private(set) var opened: [(url: URL, scheme: String)] = []
    private let respond: @MainActor (URL) throws -> URL

    init(respond: @escaping @MainActor (URL) throws -> URL) {
        self.respond = respond
    }

    convenience init(error: any Error) {
        self.init { _ in throw error }
    }

    /// Plays the provider: answers the login with a code and the request's own `state`; any other page
    /// (the end-session one) with its post-logout redirect.
    static func approving() -> FakeWeb {
        FakeWeb { url in
            let query = Dictionary(
                (URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { ($0.name, $0.value ?? "") }
            ) { first, _ in first }
            if url.path().hasSuffix("/authorize") {
                var callback = URLComponents(string: "wristcall://auth/callback")!
                callback.queryItems = [URLQueryItem(name: "code", value: "code-1"), URLQueryItem(name: "state", value: query["state"])]
                return callback.url!
            }
            return URL(string: query["post_logout_redirect_uri"] ?? "wristcall://auth/logout")!
        }
    }

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        opened.append((url, callbackScheme))
        return try respond(url)
    }
}

/// Records the waits `AccountModel` asks for (and its state at that moment), without waiting.
@MainActor
final class SleepLog {
    private(set) var delays: [TimeInterval] = []
    private(set) var states: [AccountModel.State] = []
    weak var model: AccountModel?
    /// Waits of at least this many seconds really wait: at `held`, until the test opens it.
    var holding: TimeInterval?
    let held = Gate()

    nonisolated init() {}

    func record(_ seconds: TimeInterval) {
        delays.append(seconds)
        if let model { states.append(model.state) }
    }

    func park(_ seconds: TimeInterval) async {
        if let holding, seconds >= holding { await held.wait() }
    }

    nonisolated var sleep: @Sendable (TimeInterval) async -> Void {
        { [self] seconds in
            await self.record(seconds)
            await self.park(seconds)
        }
    }
}

/// An `AccountModel` on an `AccountWorld`, with every store in memory and its own `UserDefaults`.
@MainActor
struct AccountRig {
    let model: AccountModel
    let sessions: InMemoryDeviceSessionStore
    let keys: InMemoryAccountKeyStore
    let tokens: InMemoryTokenStore
    let defaults: UserDefaults
    let sleeps: SleepLog
    let webAuthenticator: any WebAuthenticator

    var appState: AppState { model.appState }
    /// The web sheet, when it is a `FakeWeb` (the default).
    var web: FakeWeb { webAuthenticator as! FakeWeb }

    init(
        _ world: AccountWorld,
        session: StoredDeviceSession? = nil,
        keys: [StoredAccountKey] = [],
        zitadel: TokenSet? = nil,
        lastID: String? = nil,
        servers: [ManagedServer] = [],
        makeAPI: @escaping ServerAPIFactory = { _, _ in FakeServerAPI() },
        web: any WebAuthenticator = FakeWeb.approving(),
        cloudURL: URL? = nil,
        router: (any CloudRouting)? = nil,
        configure: (AppState) -> Void = { _ in }
    ) {
        sessions = InMemoryDeviceSessionStore(session)
        self.keys = InMemoryAccountKeyStore(keys)
        tokens = InMemoryTokenStore(zitadel)
        defaults = UserDefaults(suiteName: "account-tests-" + UUID().uuidString)!
        if let lastID { defaults.set(lastID, forKey: AccountModel.lastIDKey) }
        sleeps = SleepLog()
        webAuthenticator = web
        let cloud = cloudURL ?? world.cloud.url
        // What the app sends the Cloud as `X-Wristcall-Client` (the host app's Info.plist).
        let client = ClientIdentity.fromBundle(platform: .ios)
        let device = DeviceSession(cloud: cloud, store: sessions, session: .stubbed(), now: { AccountWorld.clock },
                                   client: client)
        let legacy = AccountSession(cloud: cloud, kind: .ios, store: tokens, session: .stubbed(), now: { AccountWorld.clock },
                                    client: client)
        let state = AppState(store: InMemoryManagedServerStore(servers), makeAPI: makeAPI)
        configure(state)
        model = AccountModel(
            cloudURL: cloud, session: device, keys: self.keys, legacy: legacy, web: web, state: state,
            device: CloudDeviceInfo(name: "Test iPhone", platform: .ios), defaults: defaults, sleep: sleeps.sleep,
            router: router
        )
        sleeps.model = model
    }

    /// The model, started, with the servers read.
    func started() async -> AccountModel {
        await model.appState.load()
        await model.start()
        await model.agenda?.idle()
        return model
    }

    var lastID: String? { defaults.string(forKey: AccountModel.lastIDKey) }
}

extension StoredAccountKey {
    /// A new key of `account`, stored `age` seconds ago.
    static func of(_ account: String, age: TimeInterval = 0) -> StoredAccountKey {
        StoredAccountKey(account: account, key: .generate(), createdAt: Date().addingTimeInterval(-age))
    }
}

/// A web sheet that waits at `gate` before it answers like `FakeWeb.approving()`.
@MainActor
final class GatedWeb: WebAuthenticator {
    let gate = Gate()
    private let inner = FakeWeb.approving()
    var opened: Int { inner.opened.count }

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        await gate.wait()
        return try await inner.authenticate(url: url, callbackScheme: callbackScheme)
    }
}
