import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

@MainActor
struct AccountModelTests {
    let world = AccountWorld()
    var cloudHost: StubHost { world.cloud }
    let server = ManagedServer(id: "home", name: "Home", url: URL(string: "https://srv.test")!, token: "wc_pat_home")

    /// A 0.3.x Zitadel login, valid for an hour.
    var zitadel: TokenSet {
        TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: AccountWorld.clock.addingTimeInterval(3600), idToken: "idt")
    }

    /// The server's health naming this Cloud as its account.
    var ownHealth: ServerHealth {
        ServerHealth(version: "0.6.0", account: .init(issuer: world.cloud.url.absoluteString, deviceCredential: "approval"))
    }

    /// A silent account `acc_1` with its key, signed in here.
    func readyRig(
        _ account: AccountWorld.Account = AccountWorld.Account(),
        servers: [ManagedServer] = [],
        makeAPI: @escaping ServerAPIFactory = { _, _ in FakeServerAPI() },
        web: any WebAuthenticator = FakeWeb.approving()
    ) async -> AccountRig {
        let key = StoredAccountKey.of("acc_1", age: 60)
        world.addAccount("acc_1", key: key.key, account)
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: [key], servers: servers, makeAPI: makeAPI, web: web)
        _ = await rig.started()
        return rig
    }

    func requests(_ method: String, _ path: String) -> [StubHost.Request] { world.cloudRequests(method, path) }

    // MARK: - Availability

    @Test func unavailableWithoutCloudURL() async {
        let web = FakeWeb.approving()
        let model = AccountModel(
            cloudURL: nil, session: nil, keys: InMemoryAccountKeyStore(), legacy: nil, web: web,
            state: AppState(store: InMemoryManagedServerStore()), device: CloudDeviceInfo(name: "iPhone", platform: .ios)
        )
        #expect(model.state == .unavailable)
        await model.start()
        #expect(model.state == .unavailable)
        #expect(model.agenda == nil)
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

    // MARK: - Start

    @Test func firstStartCreatesASilentAccount() async throws {
        let rig = AccountRig(world, servers: [server])
        #expect(rig.model.state == .starting)

        let model = await rig.started()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 1)
        #expect(model.info?.account == "acc_1")
        #expect(model.plan == nil)
        #expect(!model.isMigratedAccount)
        #expect(!model.didMigrate)
        #expect(model.hasLocalKey)
        let key = try #require(try rig.keys.keys().first)
        #expect(key.account == "acc_1")
        #expect(world.owner(of: key.key) == "acc_1")
        #expect(try rig.keys.pending() == nil)
        #expect(try rig.sessions.load()?.account == "acc_1")
        #expect(rig.lastID == "acc_1")
        // No sign-in page, no Zitadel: it waited once for iCloud Keychain to bring a key.
        #expect(rig.web.opened.isEmpty)
        #expect(world.endpoints.requests.isEmpty)
        #expect(rig.sleeps.delays == [2])
        // The servers are mirrored once there is an account.
        #expect(world.entries.map(\.url) == ["https://srv.test"])
        #expect(world.foreignTokenRequests.isEmpty)
    }

    @Test func startWithAStoredSessionMakesNoAccountCall() async throws {
        world.addAccount("acc_1")
        let rig = AccountRig(world, session: world.storedSession("acc_1"))
        let model = await rig.started()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(requests("POST", "/v1/devices").isEmpty)
        #expect(requests("POST", "/v1/session/refresh").isEmpty)
        let read = try #require(requests("GET", "/v1/account").first)
        #expect(read.headers["Authorization"] == "Bearer at-acc_1-0")
        #expect(model.info?.account == "acc_1")
        #expect(!model.hasLocalKey)
        #expect(rig.lastID == "acc_1")
    }

    @Test func concurrentStartsCreateOneAccount() async throws {
        let rig = AccountRig(world)
        let model = rig.model
        let first = Task { await model.start() }
        let second = Task { await model.start() }
        await first.value
        await second.value
        await model.start()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 1)
        #expect(try rig.keys.keys().count == 1)
    }

    @Test func legacyMigrationForcesAFreshZitadelToken() async throws {
        let rig = AccountRig(world, zitadel: zitadel)
        let model = await rig.started()

        #expect(model.state == .ready)
        #expect(model.didMigrate)
        #expect(model.isMigratedAccount)
        // The stored Zitadel token had an hour left: it is renewed anyway (the Cloud wants a recent `iat`).
        #expect(world.tokenRequests.count == 1)
        #expect(try world.tokenRequests.first?.form()["grant_type"] == "refresh_token")
        let signIn = try #require(requests("POST", "/v1/devices").first)
        #expect(signIn.headers["Authorization"] == "Bearer at-1")
        let put = try #require(requests("PUT", "/v1/account/key").first)
        #expect(put.headers["Authorization"]?.hasPrefix("Bearer at-acc_z-") == true)
        let key = try #require(try rig.keys.keys().first)
        #expect(key.account == AccountWorld.zitadelAccount)
        #expect(world.owner(of: key.key) == AccountWorld.zitadelAccount)
        #expect(try rig.tokens.load() == nil)
        #expect(try rig.sessions.load()?.account == AccountWorld.zitadelAccount)
        // No page opens and no new account is made.
        #expect(rig.web.opened.isEmpty)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        // Only the migration's sign-in carried the Zitadel token.
        #expect(world.foreignTokenRequests.count == 1)
    }

    @Test func deadLegacySignInAsksToSignInInsteadOfCreating() async throws {
        world.refuseRefresh = true
        let rig = AccountRig(world, zitadel: zitadel)
        let model = await rig.started()

        #expect(model.state == .needsSignIn)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(try rig.tokens.load() == nil)
        // The next launch (no Zitadel tokens any more) still never makes an account.
        await model.start()
        #expect(model.state == .needsSignIn)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(rig.web.opened.isEmpty)
    }

    @Test func startOverMakesANewAccount() async throws {
        let rig = AccountRig(world, lastID: "acc_gone")
        let model = await rig.started()
        #expect(model.state == .needsSignIn)

        await model.startOver()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 1)
        #expect(rig.lastID == "acc_1")
    }

    @Test func revokedSessionReEnrollsWithTheKey() async throws {
        let key = StoredAccountKey.of("acc_1")
        world.addAccount("acc_1", key: key.key)
        world.refuseSessionRefresh = true
        let rig = AccountRig(world, session: world.storedSession("acc_1", expired: true), keys: [key])
        let model = await rig.started()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/session/refresh").count == 1)
        let enroll = try #require(requests("POST", "/v1/devices").first)
        #expect(try enroll.json()["account_key"] as? String == key.key.value)
        #expect(enroll.headers["Authorization"] == nil)
        let session = try #require(try rig.sessions.load())
        #expect(session.account == "acc_1")
        #expect(session.device != "dev_0")
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(model.hasLocalKey)
    }

    @Test func revokedSessionWithoutKeyNeverCreatesAnAccount() async throws {
        world.addAccount("acc_1")
        world.refuseSessionRefresh = true
        let rig = AccountRig(world, session: world.storedSession("acc_1", expired: true))
        let model = await rig.started()

        #expect(model.state == .needsSignIn)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(try rig.sessions.load() == nil)
        #expect(rig.lastID == "acc_1")
    }

    @Test func revokedSessionDuringSyncGetsBackIn() async throws {
        let rig = await readyRig()
        let model = rig.model
        // The device is signed out elsewhere: the next refresh is refused.
        try rig.sessions.save(world.storedSession("acc_1", expired: true))
        world.refuseSessionRefresh = true

        _ = try await model.appState.addServer(urlText: "https://new.test", token: "wc_pat_n", name: "New")
        await model.agenda?.idle()
        await model.start()
        await model.agenda?.idle()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/devices").count == 1)
        #expect(try rig.sessions.load()?.device != "dev_0")
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(world.entries.map(\.name) == ["New"])
    }

    @Test func lateSyncedKeyReplacesAnEmptySilentAccount() async throws {
        let own = StoredAccountKey.of("acc_1")
        let other = StoredAccountKey.of("acc_2", age: 3600)
        world.addAccount("acc_1", key: own.key)
        world.addAccount("acc_2", key: other.key, AccountWorld.Account(servers: 2))
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: [own, other])
        let model = await rig.started()

        #expect(model.state == .ready)
        let enroll = try #require(requests("POST", "/v1/devices").first)
        #expect(try enroll.json()["account_key"] as? String == other.key.value)
        #expect(try enroll.json()["account"] as? String == "acc_2")
        let delete = try #require(requests("DELETE", "/v1/account").first)
        #expect(delete.headers["Authorization"] == "Bearer at-acc_1-0")
        #expect(world.account("acc_1") == nil)
        #expect(try rig.keys.keys().map(\.account) == ["acc_2"])
        #expect(try rig.sessions.load()?.account == "acc_2")
        #expect(model.info?.account == "acc_2")
        #expect(rig.lastID == "acc_2")
    }

    @Test func lateKeyLeavesAnAccountWithData() async throws {
        let own = StoredAccountKey.of("acc_1")
        let other = StoredAccountKey.of("acc_2", age: 3600)
        world.addAccount("acc_1", key: own.key, AccountWorld.Account(servers: 1))
        world.addAccount("acc_2", key: other.key)
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: [own, other])
        let model = await rig.started()

        #expect(model.info?.account == "acc_1")
        #expect(requests("POST", "/v1/devices").isEmpty)
        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(try rig.keys.keys().count == 2)
    }

    @Test func lateKeyNeedsAFreshAccountRead() async throws {
        world.addAccount("acc_1")
        let rig = AccountRig(world, session: world.storedSession("acc_1"))
        let model = await rig.started()
        #expect(model.info?.isEmpty == true)
        // A key arrives, but the account can't be read: the old (empty) `info` must not delete it.
        let other = AccountKey.generate()
        world.addAccount("acc_2", key: other, AccountWorld.Account(servers: 2))
        try rig.keys.save(other, account: "acc_2")
        world.accountReadFailure = 503

        await model.start()

        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(requests("POST", "/v1/devices").isEmpty)
        #expect(world.account("acc_1") != nil)
        #expect(try rig.sessions.load()?.account == "acc_1")

        // Read again: now it moves over.
        world.accountReadFailure = nil
        await model.start()
        #expect(requests("DELETE", "/v1/account").count == 1)
        #expect(try rig.sessions.load()?.account == "acc_2")
    }

    @Test func rateLimitedStartRetriesAfterTheDelay() async throws {
        world.signUpReplies = [StubHost.Reply(429, #"{"error":"rate_limited","message":"slow down"}"#, headers: ["Retry-After": "2"])]
        let rig = AccountRig(world)
        let model = await rig.started()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 2)
        // The wait for iCloud Keychain, then the Cloud's `Retry-After`, shown as `.waiting` meanwhile.
        #expect(rig.sleeps.delays == [2, 2])
        guard case .waiting(let retryAt) = rig.sleeps.states.last else {
            Issue.record("expected .waiting, got \(rig.sleeps.states)")
            return
        }
        #expect(retryAt != nil)
        // The same pending key went both times: one account, one key.
        let keys = requests("POST", "/v1/accounts").compactMap { try? $0.json()["account_key"] as? String }
        #expect(Set(keys).count == 1)
        #expect(try rig.keys.keys().count == 1)
    }

    /// Waits (5 s at most) until the start waits for its next try at the held sleep.
    private func waitingAtTheHeldSleep(_ rig: AccountRig) async -> Bool {
        guard await rig.sleeps.held.reached() else { return false }
        if case .waiting = rig.model.state { return true }
        return false
    }

    /// A rig whose start got a `429` with an hour of `Retry-After` and now waits (for real) for its next try.
    private func rateLimitedRig(web: any WebAuthenticator = FakeWeb.approving()) async -> (AccountRig, Task<Void, Never>) {
        world.signUpReplies = [StubHost.Reply(429, #"{"error":"rate_limited","message":"slow down"}"#, headers: ["Retry-After": "3600"])]
        let rig = AccountRig(world, web: web)
        rig.sleeps.holding = 3600
        await rig.model.appState.load()
        let model = rig.model
        let start = Task { await model.start() }
        return (rig, start)
    }

    @Test func restoreDuringRateLimitedStartDoesNotWait() async throws {
        let (rig, start) = await rateLimitedRig()
        let model = rig.model
        // A broken cut still ends the test (the start then makes an account, which fails the counts below).
        let safety = Task { try? await Task.sleep(for: .seconds(10)); await rig.sleeps.held.open() }
        defer { safety.cancel() }
        #expect(await waitingAtTheHeldSleep(rig))

        let clock = ContinuousClock()
        let began = clock.now
        try await model.restoreWithEmail()
        await start.value

        #expect(clock.now - began < .seconds(5))
        #expect(model.state == .ready)
        #expect(model.info?.account == AccountWorld.zitadelAccount)
        // The start stopped for the restore: no account was made after it.
        #expect(requests("POST", "/v1/accounts").count == 1)
        #expect(requests("DELETE", "/v1/account").isEmpty)
        await rig.sleeps.held.open()
    }

    @Test func failedRestoreDuringRateLimitedStartStartsAgain() async throws {
        // The web sheet is closed: the restore fails before any sign-in.
        let (rig, start) = await rateLimitedRig(web: FakeWeb(error: WebAuthenticatorError.couldNotStart))
        let model = rig.model
        let safety = Task { try? await Task.sleep(for: .seconds(10)); await rig.sleeps.held.open() }
        defer { safety.cancel() }
        #expect(await waitingAtTheHeldSleep(rig))

        await #expect(throws: WebAuthenticatorError.self) { try await model.restoreWithEmail() }
        await start.value
        // The stopped start runs again (the Cloud's limit is over by now): the silent account is made.
        await model.start()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 2)
        await rig.sleeps.held.open()
    }

    @Test func tryAgainCutsTheRetryWait() async throws {
        let (rig, start) = await rateLimitedRig()
        let model = rig.model
        let safety = Task { try? await Task.sleep(for: .seconds(10)); await rig.sleeps.held.open() }
        defer { safety.cancel() }
        #expect(await waitingAtTheHeldSleep(rig))

        let clock = ContinuousClock()
        let began = clock.now
        await model.tryAgain()
        await start.value

        #expect(clock.now - began < .seconds(5))
        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 2)
        await rig.sleeps.held.open()
    }

    // MARK: - Plan

    @Test func redeemTreatsAlreadyEntitledAsSuccess() async throws {
        let rig = await readyRig(AccountWorld.Account(plan: #"{"plan":"full","source":"beta"}"#))

        let plan = try await rig.model.redeemInvite("WC-AAAA-BBBB-CCCC")

        #expect(plan.plan == .full)
        #expect(plan.source == "beta")
        #expect(rig.model.plan == plan)
        #expect(requests("POST", "/v1/entitlements/redeem").count == 1)
    }

    @Test func redeemSetsThePlan() async throws {
        let rig = await readyRig()
        #expect(rig.model.plan == nil)

        let plan = try await rig.model.redeemInvite("WC-AAAA-BBBB-CCCC")

        #expect(plan == Plan(plan: .full, source: "invite"))
        #expect(rig.model.plan == plan)
        #expect(try requests("POST", "/v1/entitlements/redeem").first?.json()["code"] as? String == "WC-AAAA-BBBB-CCCC")
    }

    // MARK: - Restore

    @Test func restoreDeletesOnlyAnEmptySilentAccount() async throws {
        let rig = await readyRig()
        let model = rig.model

        try await model.restoreWithEmail()

        // The Zitadel login in the web sheet (PKCE), then a renewed token for the Cloud.
        let page = try #require(rig.web.opened.first)
        #expect(page.url.path().hasSuffix("/authorize"))
        #expect(try world.tokenRequests.map { try $0.form()["grant_type"] } == ["authorization_code", "refresh_token"])
        let signIn = try #require(requests("POST", "/v1/devices").first)
        #expect(signIn.headers["Authorization"] == "Bearer at-1")
        // The empty account goes, with its own token, and its key.
        let delete = try #require(requests("DELETE", "/v1/account").first)
        #expect(delete.headers["Authorization"] == "Bearer at-acc_1-0")
        #expect(world.account("acc_1") == nil)
        #expect(try rig.keys.keys().map(\.account) == [AccountWorld.zitadelAccount])
        #expect(try rig.sessions.load()?.account == AccountWorld.zitadelAccount)
        #expect(model.state == .ready)
        #expect(model.isMigratedAccount)
        #expect(try rig.tokens.load() == nil)
        #expect(rig.lastID == AccountWorld.zitadelAccount)
    }

    @Test func restoreWithDataNeedsConfirmation() async throws {
        let rig = await readyRig(AccountWorld.Account(servers: 2))
        let model = rig.model

        await #expect(throws: RestoreError.currentAccountHasData(servers: 2)) {
            try await model.restoreWithEmail()
        }

        #expect(try rig.sessions.load()?.account == "acc_1")
        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(model.info?.account == "acc_1")
        // The device the attempt signed in is signed out again.
        #expect(world.cloud.requests.contains { $0.method == "DELETE" && $0.path.hasPrefix("/v1/devices/") })

        try await model.restoreWithEmail(leavingCurrent: true)

        // No second page: the login of the first attempt is reused.
        #expect(rig.web.opened.count == 1)
        #expect(try rig.sessions.load()?.account == AccountWorld.zitadelAccount)
        #expect(world.account("acc_1")?.servers == 2)
        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(try Set(rig.keys.keys().map(\.account)) == ["acc_1", AccountWorld.zitadelAccount])
        #expect(model.state == .ready)
        #expect(try rig.tokens.load() == nil)
    }

    @Test func startDuringRestoreWaitsForIt() async throws {
        let web = GatedWeb()
        let rig = await readyRig(web: web)
        let model = rig.model
        let restore = Task { try await model.restoreWithEmail() }
        #expect(await web.gate.reached())
        let before = world.cloud.requests.count

        // The web sheet brings the app back to the foreground, which starts the account: it waits for the restore.
        await model.start()
        #expect(world.cloud.requests.count == before)
        let late = Task { await model.start() }
        await web.gate.open()
        try await restore.value
        await late.value
        await model.start()

        #expect(web.opened == 1)
        #expect(requests("POST", "/v1/devices").count == 1)
        #expect(requests("POST", "/v1/accounts").isEmpty)
        #expect(model.state == .ready)
        #expect(model.info?.account == AccountWorld.zitadelAccount)
        #expect(try rig.sessions.load()?.account == AccountWorld.zitadelAccount)
        #expect(try rig.keys.keys().map(\.account) == [AccountWorld.zitadelAccount])
    }

    @Test func failedRestoreKeepsTheCurrentAccount() async throws {
        let rig = await readyRig()
        let model = rig.model
        world.zitadelSignInFailure = 503

        await #expect(throws: APIError.self) { try await model.restoreWithEmail() }

        #expect(model.state == .ready)
        #expect(try rig.sessions.load()?.account == "acc_1")
        #expect(try rig.keys.keys().map(\.account) == ["acc_1"])
        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(world.account("acc_1") != nil)
    }

    @Test func restoreFromNeedsSignIn() async throws {
        let rig = AccountRig(world, lastID: "acc_gone")
        let model = await rig.started()
        #expect(model.state == .needsSignIn)

        try await model.restoreWithEmail()

        #expect(model.state == .ready)
        #expect(model.info?.account == AccountWorld.zitadelAccount)
        #expect(requests("DELETE", "/v1/account").isEmpty)
    }

    // MARK: - Sign out everywhere, watch code

    @Test func signOutEverywhereNeedsTheLocalKey() async throws {
        let rig = await readyRig()
        #expect(try await rig.model.signOutEverywhere() == 2)
        let rotate = try #require(requests("POST", "/v1/account/key/rotate").first)
        let body = try rotate.json()
        #expect(body["account_key"] as? String == body["new_account_key"] as? String)

        world.addAccount("acc_2")
        let keyless = AccountRig(world, session: world.storedSession("acc_2"))
        let model = await keyless.started()
        #expect(!model.hasLocalKey)
        await #expect(throws: AccountError.signedOut) { try await model.signOutEverywhere() }
    }

    @Test func enrollCodeForWatchIsReusedAndALimitIsNil() async throws {
        let rig = await readyRig()
        let first = try #require(await rig.model.enrollCodeForWatch())
        let again = await rig.model.enrollCodeForWatch()
        #expect(again == first)
        #expect(requests("POST", "/v1/devices/enroll-codes").count == 1)

        world.enrollLimit = true
        let other = await readyRig()
        #expect(await other.model.enrollCodeForWatch() == nil)
    }

    // MARK: - The watch's enroll code

    private func watchRig(installed: Bool = true) async -> (AccountRig, FakeWatchSession, WatchLink) {
        let rig = await readyRig()
        let session = FakeWatchSession()
        session.isWatchAppInstalled = installed
        let link = WatchLink(session: session, defaults: UserDefaults(suiteName: "AccountModelTests-\(UUID().uuidString)")!)
        rig.model.watchLink = link
        return (rig, session, link)
    }

    @Test func enrollCodeIsReusedUntilItExpires() async throws {
        let (rig, session, _) = await watchRig()
        await rig.model.publishWatchCode()
        await rig.model.publishWatchCode()
        // Reused: one code asked for, and each publish sends the same one.
        #expect(requests("POST", "/v1/devices/enroll-codes").count == 1)
        let contexts = session.contexts.compactMap(PhoneLinkContext.init)
        #expect(contexts.count == 2)
        #expect(contexts[0] == contexts[1])
        #expect(contexts[0].enrollCode.hasPrefix("code-"))
        // A code with less than a minute left is replaced.
        world.enrollTTL = 30
        let short = await readyRig()
        short.model.watchLink = WatchLink(
            session: session, defaults: UserDefaults(suiteName: "AccountModelTests-\(UUID().uuidString)")!)
        await short.model.publishWatchCode()
        await short.model.publishWatchCode()
        #expect(requests("POST", "/v1/devices/enroll-codes").count == 3)
    }

    @Test func enrollCodeLimitIsIgnored() async {
        let (rig, session, _) = await watchRig()
        world.enrollLimit = true
        await rig.model.publishWatchCode()
        #expect(session.contexts.isEmpty)
        #expect(rig.model.state == .ready)
        #expect(rig.model.error == nil)
    }

    @Test func noEnrollCodeIsAskedForWithoutTheWatchApp() async {
        let (rig, session, _) = await watchRig(installed: false)
        await rig.model.publishWatchCode()
        rig.model.publishWatchCodeInBackground()
        await rig.model.watchPublishing?.value
        #expect(requests("POST", "/v1/devices/enroll-codes").isEmpty)
        #expect(session.contexts.isEmpty)
    }

    @Test func theCodeIsPublishedWhenTheAccountBecomesReadyAndOnReturn() async throws {
        let rig = await readyRig()
        let session = FakeWatchSession()
        rig.model.watchLink = WatchLink(
            session: session, defaults: UserDefaults(suiteName: "AccountModelTests-\(UUID().uuidString)")!)
        // Back in the foreground: `start` runs again for an account that is ready.
        await rig.model.start()
        await rig.model.watchPublishing?.value
        #expect(session.contexts.count == 1)
        await rig.model.start()
        await rig.model.watchPublishing?.value
        #expect(session.contexts.count == 2)
        #expect(requests("POST", "/v1/devices/enroll-codes").count == 1)
    }

    // MARK: - Delete

    @Test func deleteAccountClearsKeysSessionAndOnboarding() async throws {
        let fake = FakeServerAPI()
        var linked = server
        linked.linked = true
        let rig = await readyRig(servers: [linked], makeAPI: { _, _ in fake })
        let model = rig.model
        // An account left behind by a restore: its key is not this account's to delete.
        let other = AccountKey.generate()
        try rig.keys.save(other, account: "acc_left")
        OnboardingStore.finish(in: rig.defaults)
        let old = StoredAccountKey.of("acc_old")
        DeadKeys.mark(old, defaults: rig.defaults)
        try rig.keys.savePending(.generate())
        var hookRan = false
        model.accountDeleted = {
            // Before the local servers go: the push keys are dropped from them.
            hookRan = model.appState.servers.count == 1
        }

        await model.deleteAccount()

        #expect(fake.calls.contains("unlinkAccount"))
        let delete = try #require(requests("DELETE", "/v1/account").first)
        #expect(delete.headers["Authorization"] == "Bearer at-acc_1-0")
        #expect(try rig.keys.keys().map(\.account) == ["acc_left"])
        #expect(try rig.keys.keys().first?.key == other)
        #expect(try rig.keys.pending() == nil)
        #expect(try rig.sessions.load() == nil)
        #expect(!OnboardingStore.isDone(in: rig.defaults))
        #expect(rig.lastID == nil)
        // The refused keys are forgotten; the key left behind is set aside, so the next start makes a new account.
        #expect(!DeadKeys.isDead(old, defaults: rig.defaults))
        #expect(try DeadKeys.isDead(#require(rig.keys.keys().first), defaults: rig.defaults))
        #expect(rig.defaults.bool(forKey: AccountModel.freshStartKey))
        #expect(hookRan)
        #expect(model.appState.servers.isEmpty)
        #expect(model.info == nil)
        #expect(model.state == .starting)
        #expect(requests("POST", "/v1/accounts").isEmpty)
    }

    @Test func startAfterDeleteMakesANewAccount() async throws {
        let rig = await readyRig()
        let model = rig.model
        // An account left behind by `restoreWithEmail(leavingCurrent: true)`: still in the Cloud, its key here.
        let left = AccountKey.generate()
        world.addAccount("acc_left", key: left)
        try rig.keys.save(left, account: "acc_left")

        await model.deleteAccount()
        await model.start()
        await model.agenda?.idle()

        #expect(model.state == .ready)
        #expect(requests("POST", "/v1/accounts").count == 1)
        let tried = requests("POST", "/v1/devices").compactMap { try? $0.json()["account_key"] as? String }
        #expect(!tried.contains(left.value))
        #expect(rig.lastID != nil)
        #expect(rig.lastID != "acc_left")
        #expect(model.info?.account == rig.lastID)
        #expect(try rig.keys.keys().map(\.account).contains("acc_left"))
        #expect(world.account("acc_left") != nil)
        #expect(!rig.defaults.bool(forKey: AccountModel.freshStartKey))
        // Back in the foreground: still the new account (C6 never moves it into the one left behind).
        await model.start()
        #expect(model.info?.account == rig.lastID)
        #expect(rig.lastID != "acc_left")
        #expect(!requests("POST", "/v1/devices").compactMap { try? $0.json()["account_key"] as? String }.contains(left.value))
    }

    @Test func failedDeleteKeepsTheAccount() async throws {
        let rig = await readyRig(servers: [server])
        let model = rig.model
        world.cloudFailure = 503
        var hookRan = false
        model.accountDeleted = { hookRan = true }

        await model.deleteAccount()

        #expect(model.state == .ready)
        #expect(model.error != nil)
        #expect(!hookRan)
        #expect(try rig.sessions.load() != nil)
        #expect(try rig.keys.keys().count == 1)
        #expect(model.appState.servers.count == 1)
    }

    @Test func deleteWithARefusedSessionSaysSo() async throws {
        let rig = await readyRig()
        let model = rig.model
        try rig.sessions.save(world.storedSession("acc_1", expired: true))
        world.refuseSessionRefresh = true

        await model.deleteAccount()

        #expect(model.error?.hasPrefix("Can't delete the account now.") == true)
        #expect(requests("DELETE", "/v1/account").isEmpty)
        #expect(world.account("acc_1") != nil)
        // Back in with the key: the account is still there.
        await model.start()
        #expect(model.state == .ready)
        #expect(model.error != nil)
        #expect(try rig.keys.keys().map(\.account) == ["acc_1"])
    }

    @Test func revokedLinkKeepsServers() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        let rig = await readyRig(servers: [server], makeAPI: { _, _ in fake })
        let model = rig.model
        // Signed out elsewhere, and no key here any more.
        try rig.sessions.save(world.storedSession("acc_1", expired: true))
        try rig.keys.deleteAll()
        world.refuseSessionRefresh = true

        await #expect(throws: AccountError.signedOut) { try await model.link(server) }
        await model.start()

        #expect(model.state == .needsSignIn)
        #expect(model.appState.servers.count == 1)
        #expect(fake.linkRequests.isEmpty)
        #expect(requests("POST", "/v1/accounts").isEmpty)
    }

    // MARK: - Link

    @Test func linkRefusesForeignIssuer() async throws {
        let fake = FakeServerAPI(health: ServerHealth(version: "0.6.0", relay: nil, account: .init(issuer: "https://evil.test", deviceCredential: "approval")))
        let model = await readyRig(makeAPI: { _, _ in fake }).model
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
        let model = await readyRig(servers: [server], makeAPI: { _, _ in fake }).model
        await #expect(throws: AccountError.serverWithoutAccount) { try await model.link(server) }
        #expect(!cloudHost.requests.contains { $0.path == "/v1/server-tokens" })
        #expect(fake.linkRequests.isEmpty)
    }

    @Test func addLinkedServerSavesApiToken() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        fake.linkResult = .success(AccountLink(linked: true, issuer: world.cloud.url.absoluteString,
                                               user: .init(id: "u1", handle: "gustavo"), apiToken: "wc_pat_new"))
        let model = await readyRig(makeAPI: { _, _ in fake }).model

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
        #expect(tokenRequest.headers["Authorization"] == "Bearer at-acc_1-0")
        // The agenda gets the server, linked.
        await model.agenda?.idle()
        #expect(model.appState.servers.first?.cloudServerID == "cs-1")
        #expect(world.entries.map(\.linked) == [true])
    }

    @Test func addLinkedServerRefusesABadCodeBeforeAnyRequest() async throws {
        let fake = FakeServerAPI(health: ownHealth)
        let model = await readyRig(makeAPI: { _, _ in fake }).model
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
        let model = await readyRig(servers: [server], makeAPI: { _, _ in fake }).model

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
        let model = await readyRig(makeAPI: { _, _ in fake }).model

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
        let model = await readyRig(servers: [server], makeAPI: { _, _ in fake }).model

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
        let model = await readyRig(makeAPI: { LiveServerAPI(server: $0, token: $1, session: .stubbed()) }).model

        let saved = try await model.addLinkedServer(urlText: host.url.absoluteString, code: "12345678", name: nil)
        try await model.link(saved)

        let links = host.requests.filter { $0.path == "/v1/account/link" }
        #expect(links.count == 2)
        for request in host.requests {
            let body = request.body.map { String(decoding: $0, as: UTF8.self) } ?? ""
            #expect(!body.contains("at-acc_1"))
            #expect(!request.headers.values.contains { $0.contains("at-acc_1") })
        }
        #expect(try links[0].json()["token"] as? String == "per-server")
        #expect(try links[0].json()["code"] as? String == "12345678")
        #expect(!links[0].headers.keys.contains { $0.lowercased() == "authorization" })
        #expect(try links[1].json()["token"] as? String == "per-server")
        #expect(links[1].headers["Authorization"] == "Bearer wc_pat_new")
    }

    // MARK: - Messages

    @Test func retryDelays() {
        #expect(AccountModel.retryDelay(for: APIError.retryLater(2)) == 2)
        #expect(AccountModel.retryDelay(for: APIError.retryLater(1e30)) == 3600)
        #expect(AccountModel.retryDelay(for: APIError.network(.notConnectedToInternet)) == 30)
        #expect(AccountModel.retryDelay(for: APIError.unavailable("down")) == 30)
        #expect(AccountModel.retryDelay(for: APIError.unauthorized) == nil)
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
