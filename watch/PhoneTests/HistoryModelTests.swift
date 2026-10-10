import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct HistoryModelTests {
    private let home = ManagedServer(id: "home", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_h")
    private let lab = ManagedServer(id: "lab", name: "Lab", url: URL(string: "https://lab.test")!, token: "wc_pat_l")

    private func loadedState(_ pairs: [(ManagedServer, FakeServerAPI)]) async -> AppState {
        let fakes = Dictionary(uniqueKeysWithValues: pairs.map { ($0.0.url.host()!, $0.1) })
        let state = AppState(store: InMemoryManagedServerStore(pairs.map(\.0)), makeAPI: { url, _ in fakes[url.host()!]! })
        await state.load()
        return state
    }

    private func model(_ pairs: [(ManagedServer, FakeServerAPI)], pageSize: Int = 30) async -> HistoryModel {
        HistoryModel(state: await loadedState(pairs), pageSize: pageSize)
    }

    private func twoServers() -> (a: FakeServerAPI, b: FakeServerAPI) {
        let a = FakeServerAPI(pages: [
            CallPage(calls: [.sample(id: "a1", at: 300), .sample(id: "a2", at: 100)], nextBefore: "100:a2"),
            CallPage(calls: [.sample(id: "a3", at: 50)], nextBefore: nil),
        ])
        let b = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "b1", at: 200)], nextBefore: nil)])
        return (a, b)
    }

    // MARK: Joining servers

    @Test func mergesServersByDate() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        #expect(model.items.map(\.call.id) == ["a1", "b1", "a2"])
        #expect(model.items.map(\.serverName) == ["Home", "Lab", "Home"])
        #expect(model.canLoadMore)
        await model.loadMore()
        #expect(a.queries.last?.before == "100:a2")
        #expect(b.queries.count == 1)
        #expect(model.items.map(\.call.id) == ["a1", "b1", "a2", "a3"])
        #expect(!model.canLoadMore)
    }

    @Test func sameIDOnTwoServersStaysTwoItems() async {
        let a = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "x", at: 10)])])
        let b = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "x", at: 10)])])
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        #expect(model.items.count == 2)
        #expect(Set(model.items.map(\.id)).count == 2)
    }

    @Test func tieOnDateBreaksByID() async {
        let a = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "c1", at: 10), .sample(id: "c2", at: 10)])])
        let b = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "c3", at: 10)])])
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        #expect(model.items.map(\.call.id) == ["c3", "c2", "c1"])
    }

    @Test func pageSizeReachesTheQuery() async {
        let (a, _) = twoServers()
        let model = await model([(home, a)], pageSize: 7)
        await model.reload()
        #expect(a.queries.first?.limit == 7)
        #expect(a.queries.first?.before == nil)
    }

    @Test func oneServerDownKeepsOthers() async {
        let (a, b) = twoServers()
        b.callsError = APIError.unavailable("The server is busy.")
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        #expect(model.items.map(\.call.id) == ["a1", "a2"])
        #expect(model.failures == ["lab": "The server is busy."])
        #expect(model.canLoadMore)
        b.callsError = nil
        await model.reload()
        #expect(model.failures.isEmpty)
        #expect(model.items.map(\.call.id) == ["a1", "b1", "a2"])
    }

    @Test func failedReloadKeepsWhatThatServerHadForTheSameFilter() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        b.callsError = APIError.network(.notConnectedToInternet)
        await model.reload()
        #expect(model.items.map(\.call.id) == ["a1", "b1", "a2"])
        #expect(model.failures["lab"] != nil)
    }

    @Test func failedReloadDropsOldFilterItems() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        b.callsError = APIError.network(.notConnectedToInternet)
        model.filter.text = "milk"
        await model.reload()
        #expect(model.items.map(\.call.id) == ["a1", "a2"])
    }

    @Test func failedLoadMoreKeepsCursorForRetry() async {
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        await model.reload()
        a.callsError = APIError.rateLimited
        await model.loadMore()
        #expect(model.failures["home"] == APIError.rateLimited.message)
        #expect(model.canLoadMore)
        a.callsError = nil
        await model.loadMore()
        #expect(model.items.map(\.call.id) == ["a1", "a2", "a3"])
        #expect(model.failures.isEmpty)
    }

    @Test func loadMoreDoesNotDuplicate() async {
        let a = FakeServerAPI(pages: [
            CallPage(calls: [.sample(id: "a1", at: 300)], nextBefore: "300:a1"),
            CallPage(calls: [.sample(id: "a1", at: 300), .sample(id: "a2", at: 100)]),
        ])
        let model = await model([(home, a)])
        await model.reload()
        await model.loadMore()
        #expect(model.items.map(\.call.id) == ["a1", "a2"])
    }

    @Test func loadMoreKeepsTheFilterTheListWasLoadedWith() async {
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        model.filter.text = "milk"
        await model.reload()
        model.filter.text = "typed but not searched yet"
        await model.loadMore()
        #expect(a.queries.last?.before == "100:a2")
        #expect(a.queries.last?.text == "milk")
    }

    @Test func noServersIsEmptyAndQuiet() async {
        let model = await model([])
        await model.reload()
        #expect(model.items.isEmpty)
        #expect(!model.canLoadMore)
        await model.loadMore()
        #expect(model.failures.isEmpty)
    }

    // MARK: Filters

    @Test func searchPassesText() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        model.filter.text = "milk"
        await model.reload()
        #expect(a.queries.last?.text == "milk")
        #expect(b.queries.last?.text == "milk")
    }

    @Test func serverFilterAsksOnlyThatServer() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        model.filter.serverID = "lab"
        await model.reload()
        #expect(a.queries.isEmpty)
        #expect(b.queries.count == 1)
        #expect(model.items.map(\.call.id) == ["b1"])
    }

    @Test func agentFilterNeedsServer() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        model.filter.agentID = "notes"
        await model.reload()
        #expect(a.queries.last?.agent == nil)
        #expect(b.queries.last?.agent == nil)
        model.filter.serverID = "home"
        await model.reload()
        #expect(a.queries.last?.agent == "notes")
    }

    @Test func periodReachesTheQuery() async {
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        let since = Date(timeIntervalSince1970: 1_000)
        let until = Date(timeIntervalSince1970: 2_000)
        model.filter.since = since
        model.filter.until = until
        await model.reload()
        #expect(a.queries.last?.since == since)
        #expect(a.queries.last?.until == until)
    }

    @Test func selectingServerLoadsItsAgentsAndClearsAgent() async {
        let (a, b) = twoServers()
        a.agentList = [.sample(slug: "notes"), .sample(slug: "todo", displayName: "Todo")]
        let model = await model([(home, a), (lab, b)])
        model.filter.agentID = "stale"
        model.selectServer("home")
        #expect(model.filter.serverID == "home")
        #expect(model.filter.agentID == nil)
        await model.loadAgents()
        #expect(model.agents.map(\.slug) == ["notes", "todo"])
        model.selectServer(nil)
        await model.loadAgents()
        #expect(model.agents.isEmpty)
    }

    @Test func selectingAnotherServerKeepsAgentOnlyWhenSame() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        model.selectServer("home")
        model.filter.agentID = "notes"
        model.selectServer("home")
        #expect(model.filter.agentID == "notes")
        model.selectServer("lab")
        #expect(model.filter.agentID == nil)
    }

    @Test func agentListFailureIsShownNotThrown() async {
        let (a, _) = twoServers()
        a.agentsError = APIError.unauthorized
        let model = await model([(home, a)])
        model.selectServer("home")
        await model.loadAgents()
        #expect(model.agents.isEmpty)
        #expect(model.error == APIError.unauthorized.message)
    }

    // MARK: Actions

    @Test func deleteRemovesItem() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        await model.delete(model.items[1])
        #expect(b.deletedCalls == ["b1"])
        #expect(model.items.map(\.call.id) == ["a1", "a2"])
        #expect(model.error == nil)
    }

    @Test func deleteFailureKeepsItem() async {
        let (a, _) = twoServers()
        a.deleteCallError = APIError.unavailable("Try later.")
        let model = await model([(home, a)])
        await model.reload()
        await model.delete(model.items[0])
        #expect(model.items.count == 2)
        #expect(model.error == "Try later.")
    }

    @Test func deleteOfVanishedCallDropsIt() async {
        let (a, _) = twoServers()
        a.deleteCallError = APIError.notFound
        let model = await model([(home, a)])
        await model.reload()
        await model.delete(model.items[0])
        #expect(model.items.map(\.call.id) == ["a2"])
        #expect(model.error == nil)
    }

    @Test func deleteAllByAgent() async {
        let (a, b) = twoServers()
        a.deleteAllResult = 2
        let model = await model([(home, a), (lab, b)])
        await model.reload()
        model.selectServer("home")
        model.filter.agentID = "notes"
        await model.deleteAll()
        #expect(a.deleteAllRequests == ["notes"])
        #expect(b.deleteAllRequests.isEmpty)
        #expect(model.notice == "Deleted 2 calls.")
        // The list is read again for the filter that is on.
        #expect(a.queries.count >= 2)
    }

    @Test func deleteAllOfAServerHasNoAgent() async {
        let (a, _) = twoServers()
        a.deleteAllResult = 1
        let model = await model([(home, a)])
        model.selectServer("home")
        await model.deleteAll()
        #expect(a.deleteAllRequests == [nil])
        #expect(model.notice == "Deleted 1 call.")
    }

    @Test func deleteAllNeedsAServer() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await model.deleteAll()
        #expect(a.deleteAllRequests.isEmpty)
        #expect(b.deleteAllRequests.isEmpty)
    }

    @Test func deleteAllIgnoresSearchAndPeriod() async {
        // The confirmation promises "every call of this agent": the search text must not narrow it silently.
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        model.selectServer("home")
        model.filter.text = "milk"
        model.filter.since = Date(timeIntervalSince1970: 5)
        await model.deleteAll()
        #expect(a.deleteAllRequests == [nil])
    }

    @Test func deleteAllFailureShowsMessage() async {
        let (a, _) = twoServers()
        a.deleteAllError = APIError.forbidden("Not allowed.")
        let model = await model([(home, a)])
        await model.reload()
        model.selectServer("home")
        await model.deleteAll()
        #expect(model.error == "Not allowed.")
        #expect(model.notice == nil)
    }

    @Test func redeliverReplacesItem() async {
        let failed = CallRecord.sample(id: "a1", at: 300, status: "failed", error: "delivery_failed")
        let a = FakeServerAPI(pages: [CallPage(calls: [failed, .sample(id: "a2", at: 100)])])
        a.redeliverResult = .sample(id: "a1", at: 300, status: "processing")
        let model = await model([(home, a)])
        await model.reload()
        #expect(model.items[0].call.canRedeliver)
        await model.redeliver(model.items[0])
        #expect(a.redelivered == ["a1"])
        #expect(model.items.map(\.call.id) == ["a1", "a2"])
        #expect(model.items[0].call.status == "processing")
        #expect(model.items[0].serverName == "Home")
    }

    @Test func redeliverRefusalShowsServerReason() async {
        let failed = CallRecord.sample(id: "a1", status: "failed", error: "delivery_failed")
        let a = FakeServerAPI(pages: [CallPage(calls: [failed])])
        a.redeliverError = APIError.conflict(code: "busy", message: "Already delivering.")
        let model = await model([(home, a)])
        await model.reload()
        await model.redeliver(model.items[0])
        #expect(model.error == "Already delivering.")
        #expect(model.items[0].call.status == "failed")
    }

    @Test func redeliverOnlyWhenTheCallAllowsIt() async {
        let delivered = CallRecord.sample(id: "a1")
        let a = FakeServerAPI(pages: [CallPage(calls: [delivered])])
        a.redeliverResult = .sample(id: "a1", status: "processing")
        let model = await model([(home, a)])
        await model.reload()
        await model.redeliver(model.items[0])
        #expect(a.redelivered.isEmpty)
    }

    @Test func refreshReplacesItemWithServerCopy() async {
        let a = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "a1", status: "processing")])])
        a.singleCalls["a1"] = .sample(id: "a1", status: "delivered")
        let model = await model([(home, a)])
        await model.reload()
        await model.refresh(model.items[0])
        #expect(model.items[0].call.status == "delivered")
    }

    @Test func refreshOfVanishedCallDropsIt() async {
        let a = FakeServerAPI(pages: [CallPage(calls: [.sample(id: "a1")])])
        a.callError = APIError.notFound
        let model = await model([(home, a)])
        await model.reload()
        await model.refresh(model.items[0])
        #expect(model.items.isEmpty)
    }

    // MARK: Export

    @Test func exportWritesFile() async throws {
        let (a, b) = twoServers()
        b.exportResult = HistoryExport(filename: "lab-history.json", data: Data("[1]".utf8))
        let model = await model([(home, a), (lab, b)])
        model.selectServer("lab")
        let url = try await model.export(.json)
        defer { model.discardExport() }
        #expect(url.lastPathComponent == "lab-history.json")
        #expect(url.path.hasPrefix(FileManager.default.temporaryDirectory.resolvingSymlinksInPath().path)
            || url.path.hasPrefix(FileManager.default.temporaryDirectory.path))
        #expect(try Data(contentsOf: url) == Data("[1]".utf8))
        #expect(b.exports.map(\.format) == [.json])
        #expect(a.exports.isEmpty)
    }

    @Test func exportNeedsAServer() async {
        let (a, b) = twoServers()
        let model = await model([(home, a), (lab, b)])
        await #expect(throws: HistoryModel.ExportError.chooseServer) { try await model.export(.markdown) }
        #expect(a.exports.isEmpty)
    }

    @Test func exportPassesPeriod() async throws {
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        model.selectServer("home")
        model.filter.agentID = "notes"
        model.filter.since = Date(timeIntervalSince1970: 1_000)
        model.filter.until = Date(timeIntervalSince1970: 2_000)
        _ = try await model.export(.markdown)
        defer { model.discardExport() }
        #expect(a.exports == [.init(format: .markdown, agent: "notes",
                                    since: Date(timeIntervalSince1970: 1_000), until: Date(timeIntervalSince1970: 2_000))])
    }

    @Test func exportFailureThrowsServerMessage() async {
        let (a, _) = twoServers()
        a.exportError = APIError.unavailable("Export is busy.")
        let model = await model([(home, a)])
        model.selectServer("home")
        await #expect(throws: HistoryModel.ExportError.failed("Export is busy.")) { try await model.export(.markdown) }
    }

    @Test func secondExportRemovesTheFirstFile() async throws {
        let (a, _) = twoServers()
        let model = await model([(home, a)])
        model.selectServer("home")
        let first = try await model.export(.markdown)
        let second = try await model.export(.markdown)
        defer { model.discardExport() }
        #expect(!FileManager.default.fileExists(atPath: first.path))
        #expect(FileManager.default.fileExists(atPath: second.path))
    }

    @Test func exportNeverEscapesTheTemporaryDirectory() async throws {
        let (a, _) = twoServers()
        a.exportResult = HistoryExport(filename: "../../evil.md", data: Data("x".utf8))
        let model = await model([(home, a)])
        model.selectServer("home")
        let url = try await model.export(.markdown)
        defer { model.discardExport() }
        #expect(url.lastPathComponent == "evil.md")
        #expect(!url.pathComponents.contains(".."))
    }

    // MARK: Transcript

    @Test func transcriptLabelsSpeakersAndErrors() {
        let call = CallRecord.sample(entries: [
            CallEntry(role: "user", text: "hello", at: 1),
            CallEntry(role: "assistant", text: "hi there", at: 2),
            CallEntry(role: "user", error: "stt_failed", at: 3),
        ])
        #expect(call.transcript == "You: hello\nAgent: hi there\nYou: (stt_failed)")
    }

    @Test func transcriptFallsBackToText() {
        #expect(CallRecord.sample(text: "buy milk").transcript == "buy milk")
        #expect(CallRecord.sample(text: nil).transcript == "")
    }
}

extension HistoryModelTests {
    @Test func periodPresetSetsSinceFromNow() async {
        let model = await model([])
        let now = Date(timeIntervalSince1970: 1_000_000)
        model.setPeriod(.week, now: now)
        #expect(model.period == .week)
        #expect(model.filter.since == now.addingTimeInterval(-7 * 86_400))
        #expect(model.filter.until == nil)
        model.setPeriod(.anytime, now: now)
        #expect(model.filter.since == nil)
    }
}
