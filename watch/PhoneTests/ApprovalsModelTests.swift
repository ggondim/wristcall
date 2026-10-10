import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct ApprovalsModelTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_000_000)
    private let now: () -> Date = { ApprovalsModelTests.clock }
    private let home = ManagedServer(id: "home", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_h")
    private let lab = ManagedServer(id: "lab", name: "Lab", url: URL(string: "https://lab.test")!, token: "wc_pat_l")
    private let account = ServerHealth.AccountInfo(issuer: "https://cloud.test", deviceCredential: "approval")

    private func request(_ id: String, _ name: String = "Watch", in seconds: Double = 300) -> ApprovalRequest {
        ApprovalRequest(requestId: id, deviceName: name, expiresAt: Self.clock.timeIntervalSince1970 + seconds)
    }

    /// An `AppState` over the given servers, each answering through its own fake; `load()` ran, so the
    /// health (and with it `account`) is known.
    private func loadedState(_ pairs: [(ManagedServer, FakeServerAPI)], account hasAccount: Bool = true) async -> AppState {
        let fakes = Dictionary(uniqueKeysWithValues: pairs.map { ($0.0.url.host()!, $0.1) })
        for (_, fake) in pairs {
            fake.healthResult = .success(ServerHealth(version: "1.0.0", account: hasAccount ? account : nil))
        }
        let state = AppState(store: InMemoryManagedServerStore(pairs.map(\.0)), makeAPI: { url, _ in fakes[url.host()!]! })
        await state.load()
        return state
    }

    private func model(_ pairs: [(ManagedServer, FakeServerAPI)], account: Bool = true) async -> ApprovalsModel {
        ApprovalsModel(state: await loadedState(pairs, account: account), now: now)
    }

    // MARK: handle (notification actions)

    @Test func handleRejectsMalformedRequestID() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        for bad in ["12a4", "../x", "", "123", "12345", "1234\n", " 1234", "１２３４", "12/4", "1234/approve"] {
            #expect(await model.handle(action: .approve, serverID: home.id, requestID: bad) == false)
            #expect(await model.handle(action: .deny, serverID: home.id, requestID: bad) == false)
        }
        #expect(!fake.calls.contains("approve"))
        #expect(!fake.calls.contains("deny"))
    }

    @Test func handleUnknownServerDoesNothing() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        let before = fake.calls
        #expect(await model.handle(action: .approve, serverID: "other", requestID: "0423") == false)
        #expect(await model.handle(action: .deny, serverID: "", requestID: "0423") == false)
        #expect(fake.calls == before)
    }

    @Test func handleLoadsStoreWhenCold() async {
        // The app was launched by the notification action: `load()` did not run, so nothing is in memory.
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let state = AppState(store: InMemoryManagedServerStore([home]), makeAPI: { _, _ in fake })
        let model = ApprovalsModel(state: state, now: now)
        #expect(state.servers.isEmpty)
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == true)
        #expect(fake.approvedRequests == ["0423"])
        #expect(fake.calls == ["pairingRequests", "approve"])
    }

    @Test func handleDenySendsDeny() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .deny, serverID: home.id, requestID: "0423") == true)
        #expect(fake.deniedRequests == ["0423"])
        #expect(fake.approvedRequests.isEmpty)
    }

    @Test func handleExpiredRequestDoesNotCallServer() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        let before = fake.calls
        let past = Self.clock.addingTimeInterval(-1)
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423", expiresAt: past) == false)
        #expect(model.notice == "This request expired.")
        #expect(await model.handle(action: .deny, serverID: home.id, requestID: "0423", expiresAt: Self.clock) == false)
        #expect(fake.calls == before)
    }

    @Test func handleUnexpiredRequestGoesThrough() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        let later = Self.clock.addingTimeInterval(60)
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423", expiresAt: later) == true)
        #expect(fake.approvedRequests == ["0423"])
        #expect(model.notice == nil)
    }

    @Test func handleServerSideExpiryShowsNotice() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        fake.approveError = APIError.notFound
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == false)
        #expect(model.notice == "This request expired.")
    }

    @Test func handleDeviceLimitShowsMessage() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        fake.approveError = APIError.limit("You reached the limit of 5 devices.")
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == false)
        #expect(model.notice == "You reached the limit of 5 devices.")
    }

    @Test func handleRemovesTheItemFromThePendingList() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423"), request("0777")]
        let model = await model([(home, fake)])
        await model.refresh()
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == true)
        #expect(model.pending.map(\.request.requestId) == ["0777"])
    }

    // MARK: refresh

    @Test func refreshMergesServers() async {
        let homeFake = FakeServerAPI()
        homeFake.requestList = [request("0423", "Watch A", in: 200)]
        let labFake = FakeServerAPI()
        labFake.requestsError = APIError.notConfigured
        let thirdFake = FakeServerAPI()
        thirdFake.requestList = [request("0423", "Watch B", in: 100)]
        let third = ManagedServer(id: "third", name: "Third", url: URL(string: "https://third.test")!, token: "wc_pat_t")
        let model = await model([(home, homeFake), (lab, labFake), (third, thirdFake)])
        await model.refresh()
        #expect(model.pending.map(\.id) == ["home/0423", "third/0423"])
        #expect(model.pending.map(\.serverName) == ["Home", "Third"])
        #expect(model.pending.map(\.request.deviceName) == ["Watch A", "Watch B"])
        #expect(model.notice == nil)
    }

    @Test func refreshOnlyAsksServersWithAnAccount() async {
        // An old server answers FastAPI's 404, not `not_configured`: it is not even asked.
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)], account: false)
        await model.refresh()
        #expect(!fake.calls.contains("pairingRequests"))
        #expect(model.pending.isEmpty)
    }

    @Test func refreshSkipsServerWhoseHealthIsUnknown() async {
        let fake = FakeServerAPI()
        fake.healthError = APIError.network(.notConnectedToInternet)
        fake.requestList = [request("0423")]
        let state = AppState(store: InMemoryManagedServerStore([home]), makeAPI: { _, _ in fake })
        await state.load()
        let model = ApprovalsModel(state: state, now: now)
        await model.refresh()
        #expect(!fake.calls.contains("pairingRequests"))
    }

    @Test func refreshDropsExpiredAndMalformedRequests() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423"), request("0001", in: 0), request("0002", in: -5), request("12a4"), request("../x")]
        let model = await model([(home, fake)])
        await model.refresh()
        #expect(model.pending.map(\.request.requestId) == ["0423"])
    }

    @Test func refreshKeepsItemsOnATransientError() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        fake.requestsError = APIError.network(.notConnectedToInternet)
        await model.refresh()
        #expect(model.pending.map(\.request.requestId) == ["0423"])
    }

    @Test func refreshDropsItemsWhenTheServerNoLongerAcceptsTheToken() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        fake.requestsError = APIError.unauthorized
        await model.refresh()
        #expect(model.pending.isEmpty)
    }

    @Test func refreshDropsItemsOfRemovedServers() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let state = await loadedState([(home, fake)])
        let model = ApprovalsModel(state: state, now: now)
        await model.refresh()
        await state.remove(home.id)
        await model.refresh()
        #expect(model.pending.isEmpty)
    }

    @Test func refreshDoesNotBringBackWhatWasJustAnswered() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        let item = model.pending[0]
        // The list was read, then the user taps Approve before the refresh finished.
        fake.afterListing = { @Sendable in await Task { @MainActor in await model.approve(item) }.value }
        await model.refresh()
        fake.afterListing = nil
        #expect(fake.approvedRequests == ["0423"])
        #expect(model.pending.isEmpty)
    }

    // MARK: approve and deny

    @Test func approveRemovesItem() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423"), request("0777")]
        let model = await model([(home, fake)])
        await model.refresh()
        await model.approve(model.pending[0])
        #expect(fake.approvedRequests == ["0423"])
        #expect(model.pending.map(\.request.requestId) == ["0777"])
        #expect(model.notice == nil)
    }

    @Test func denyRemovesItem() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423"), request("0777")]
        let model = await model([(home, fake)])
        await model.refresh()
        await model.deny(model.pending[1])
        #expect(fake.deniedRequests == ["0777"])
        #expect(fake.approvedRequests.isEmpty)
        #expect(model.pending.map(\.request.requestId) == ["0423"])
    }

    @Test func approveGoesToTheServerOfTheItem() async {
        let homeFake = FakeServerAPI()
        homeFake.requestList = [request("0423")]
        let labFake = FakeServerAPI()
        labFake.requestList = [request("0423")]
        let model = await model([(home, homeFake), (lab, labFake)])
        await model.refresh()
        await model.approve(model.pending.first { $0.serverID == "lab" }!)
        #expect(labFake.approvedRequests == ["0423"])
        #expect(homeFake.approvedRequests.isEmpty)
        #expect(model.pending.map(\.id) == ["home/0423"])
    }

    @Test func approveExpiredShowsNotice() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        fake.approveError = APIError.notFound
        await model.approve(model.pending[0])
        #expect(model.notice == "This request expired.")
        #expect(model.pending.isEmpty)
    }

    @Test func approveBeyondDeviceLimitShowsMessageAndKeepsItem() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        fake.approveError = APIError.limit("You reached the limit of 5 devices.")
        await model.approve(model.pending[0])
        #expect(model.notice == "You reached the limit of 5 devices.")
        #expect(model.pending.count == 1)
    }

    @Test func denyExpiredShowsNotice() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let model = await model([(home, fake)])
        await model.refresh()
        fake.denyError = APIError.notFound
        await model.deny(model.pending[0])
        #expect(model.notice == "This request expired.")
        #expect(model.pending.isEmpty)
    }

    @Test func approveOfARemovedServerSendsNothing() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423")]
        let state = await loadedState([(home, fake)])
        let model = ApprovalsModel(state: state, now: now)
        await model.refresh()
        let item = model.pending[0]
        await state.remove(home.id)
        await model.approve(item)
        #expect(fake.approvedRequests.isEmpty)
        #expect(model.pending.isEmpty)
    }

    @Test func approveWithMalformedIDSendsNothing() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        let item = ApprovalsModel.Pending(serverID: home.id, serverName: "Home", request: request("../x"))
        await model.approve(item)
        await model.deny(item)
        #expect(fake.approvedRequests.isEmpty)
        #expect(fake.deniedRequests.isEmpty)
    }

    // MARK: handle needs a known expiry

    @Test func handleWithoutAnyExpiryDoesNotApprove() async {
        // Neither the push nor the server's list knows this id: a stale action must not hit a newer request.
        let fake = FakeServerAPI()
        fake.requestList = [request("0777")]
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == false)
        #expect(await model.handle(action: .deny, serverID: home.id, requestID: "0423") == false)
        #expect(!fake.calls.contains("approve"))
        #expect(!fake.calls.contains("deny"))
        #expect(model.notice == "This request expired.")
    }

    @Test func handleWithoutPushExpiryUsesTheListedOne() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423", in: 120)]
        let model = await model([(home, fake)])
        let before = fake.calls
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == true)
        #expect(Array(fake.calls.dropFirst(before.count)) == ["pairingRequests", "approve"])
    }

    @Test func handleWithoutPushExpiryRefusesAListedExpiredRequest() async {
        let fake = FakeServerAPI()
        fake.requestList = [request("0423", in: -1)]
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == false)
        #expect(!fake.calls.contains("approve"))
        #expect(model.notice == "This request expired.")
    }

    @Test func handleWithoutPushExpiryRefusesWhenTheListFails() async {
        let fake = FakeServerAPI()
        fake.requestsError = APIError.network(.notConnectedToInternet)
        let model = await model([(home, fake)])
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423") == false)
        #expect(!fake.calls.contains("approve"))
        #expect(model.notice == APIError.network(.notConnectedToInternet).message)
    }

    @Test func handleWithPushExpiryDoesNotNeedTheList() async {
        let fake = FakeServerAPI()
        let model = await model([(home, fake)])
        let later = Self.clock.addingTimeInterval(60)
        #expect(await model.handle(action: .approve, serverID: home.id, requestID: "0423", expiresAt: later) == true)
        #expect(!fake.calls.contains("pairingRequests"))
    }
}
