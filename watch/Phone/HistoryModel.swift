import Foundation
import Observation
import WristcallKit

/// The calls of every server in one list, newest first. Each server is asked for its own page and the pages are
/// joined by date; a server that does not answer is named in `failures` and does not hide the others.
@MainActor
@Observable
final class HistoryModel {
    struct Item: Identifiable, Equatable {
        let serverID: String
        let serverName: String
        var call: CallRecord
        /// The same call id can exist on two servers, so the server is part of the identity.
        var id: String { serverID + "/" + call.id }
    }

    struct Filter: Equatable {
        var serverID: String?
        /// An agent id or slug of the chosen server; ignored while no server is chosen.
        var agentID: String?
        var text: String = ""
        var since: Date?
        var until: Date?

        var isActive: Bool {
            serverID != nil || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || since != nil || until != nil
        }
    }

    /// Ready made periods for the filter menu (a window that ends now).
    enum Period: String, CaseIterable, Identifiable {
        case anytime, day, week, month

        var id: String { rawValue }

        var title: String {
            switch self {
            case .anytime: "Any time"
            case .day: "Last 24 hours"
            case .week: "Last 7 days"
            case .month: "Last 30 days"
            }
        }

        func since(now: Date) -> Date? {
            switch self {
            case .anytime: nil
            case .day: now.addingTimeInterval(-86_400)
            case .week: now.addingTimeInterval(-7 * 86_400)
            case .month: now.addingTimeInterval(-30 * 86_400)
            }
        }
    }

    enum ExportError: Error, Equatable {
        /// An export is the file of one server.
        case chooseServer
        case failed(String)

        var message: String {
            switch self {
            case .chooseServer: "Choose one server to export its history."
            case .failed(let text): text
            }
        }
    }

    /// Newest first; a tie on the date is broken by the call id (greatest first), like the servers' own cursor.
    private(set) var items: [Item] = []
    private(set) var canLoadMore = false
    /// Why a server's page is missing, by server id.
    private(set) var failures: [String: String] = [:]
    private(set) var isLoading = false
    private(set) var isLoadingMore = false
    /// The agents of the chosen server (the filter menu); empty while no server is chosen.
    private(set) var agents: [AgentDetail] = []
    private(set) var period: Period = .anytime
    var filter = Filter()
    /// What the last action could not do. `nil` clears it.
    var error: String?
    /// What the last "delete all" did. `nil` clears it.
    var notice: String?

    @ObservationIgnored private let state: AppState
    @ObservationIgnored private let pageSize: Int
    @ObservationIgnored private var records: [String: [CallRecord]] = [:]
    @ObservationIgnored private var cursors: [String: String] = [:]
    @ObservationIgnored private var names: [String: String] = [:]
    /// The filter the list on screen was read with: "load more" continues it, whatever `filter` says by now.
    @ObservationIgnored private var loadedFilter: Filter?
    /// Counts reloads; an answer that arrives after a newer reload started belongs to an old filter.
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var exportDirectory: URL?

    init(state: AppState, pageSize: Int = 30) {
        self.state = state
        self.pageSize = pageSize
    }

    // MARK: Reading

    /// First page of every server the filter covers, in parallel.
    func reload() async {
        generation += 1
        let mine = generation
        isLoading = true
        defer { if generation == mine { isLoading = false } }

        let filter = filter
        let servers = servers(for: filter)
        let results = await Self.fetch(servers.map { ($0.id, state.api(for: $0), query(filter, before: nil)) })
        guard !Task.isCancelled, generation == mine else { return }

        let keepOld = loadedFilter == filter
        var newRecords: [String: [CallRecord]] = [:]
        var newCursors: [String: String] = [:]
        var newFailures: [String: String] = [:]
        for server in servers {
            switch results[server.id] {
            case .success(let page):
                newRecords[server.id] = page.calls
                if let next = page.nextBefore { newCursors[server.id] = next }
            case .failure(let error):
                newFailures[server.id] = APIError.text(error)
                // A blip on a refresh keeps what that server showed; another filter's calls would mislead.
                if keepOld {
                    newRecords[server.id] = records[server.id] ?? []
                    newCursors[server.id] = cursors[server.id]
                }
            case nil:
                break
            }
        }
        records = newRecords
        cursors = newCursors
        failures = newFailures
        names = Dictionary(uniqueKeysWithValues: servers.map { ($0.id, $0.name) })
        loadedFilter = filter
        rebuild()
    }

    /// The next page of every server that still has one, joined into the list.
    func loadMore() async {
        guard !isLoadingMore, let loaded = loadedFilter else { return }
        let mine = generation
        let ids = cursors.keys.sorted()
        let jobs = ids.compactMap { id -> (String, any ServerAPI, HistoryQuery)? in
            guard let server = state.servers.first(where: { $0.id == id }), let before = cursors[id] else { return nil }
            return (id, state.api(for: server), query(loaded, before: before))
        }
        guard !jobs.isEmpty else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }

        let results = await Self.fetch(jobs)
        guard !Task.isCancelled, generation == mine else { return }
        for (id, _, _) in jobs {
            switch results[id] {
            case .success(let page):
                var known = records[id] ?? []
                let seen = Set(known.map(\.id))
                known += page.calls.filter { !seen.contains($0.id) }
                records[id] = known
                cursors[id] = page.nextBefore
                failures[id] = nil
            case .failure(let error):
                failures[id] = APIError.text(error)
            case nil:
                break
            }
        }
        rebuild()
    }

    /// Chooses the server of the filter; the agent only makes sense inside one server, so it is dropped when
    /// the server changes.
    func selectServer(_ id: String?) {
        if filter.serverID != id {
            filter.agentID = nil
            agents = []
        }
        filter.serverID = id
    }

    /// Reads the agents of the chosen server for the filter menu.
    func loadAgents() async {
        guard let id = filter.serverID, let server = state.servers.first(where: { $0.id == id }) else {
            agents = []
            return
        }
        do {
            let list = try await state.api(for: server).agents()
            if !Task.isCancelled, filter.serverID == id { agents = list }
        } catch {
            if !Task.isCancelled, filter.serverID == id { self.error = APIError.text(error) }
        }
    }

    func setPeriod(_ period: Period, now: Date = Date()) {
        self.period = period
        filter.since = period.since(now: now)
        filter.until = nil
    }

    // MARK: Changing

    func delete(_ item: Item) async {
        error = nil
        guard let api = api(for: item) else { return }
        do {
            try await api.deleteCall(item.call.id)
        } catch APIError.notFound {
            // Deleted from elsewhere meanwhile: same result.
        } catch {
            self.error = APIError.text(error)
            return
        }
        records[item.serverID]?.removeAll { $0.id == item.call.id }
        rebuild()
    }

    /// Deletes every call of the chosen server, or of the chosen agent in it. Search and period do not narrow
    /// it: the confirmation on screen promises the whole agent. Needs a server.
    func deleteAll() async {
        error = nil
        notice = nil
        guard let id = filter.serverID, let server = state.servers.first(where: { $0.id == id }) else { return }
        var agent: String?
        if let chosen = filter.agentID {
            agent = chosen.trimmingCharacters(in: .whitespacesAndNewlines)
            // A blank agent would read as "all": refuse it here.
            guard agent?.isEmpty == false else {
                error = "Choose an agent."
                return
            }
        }
        do {
            let count = try await state.api(for: server).deleteCalls(agent: agent)
            notice = count == 1 ? "Deleted 1 call." : "Deleted \(count) calls."
        } catch {
            self.error = APIError.text(error)
            return
        }
        await reload()
    }

    /// Sends a failed one-way call again. The list shows the answer (the call, `processing` now).
    func redeliver(_ item: Item) async {
        error = nil
        guard item.call.canRedeliver, let api = api(for: item) else { return }
        do {
            replace(item, with: try await api.redeliver(item.call.id))
        } catch {
            self.error = APIError.text(error)
        }
    }

    /// Reads the call again (follows a redelivery). A blip is not worth an alert: the next try is soon.
    func refresh(_ item: Item) async {
        guard let api = api(for: item) else { return }
        do {
            replace(item, with: try await api.call(item.call.id))
        } catch APIError.notFound {
            records[item.serverID]?.removeAll { $0.id == item.call.id }
            rebuild()
        } catch {
            // Keep what is on screen.
        }
    }

    // MARK: Export

    /// The server's own file for the chosen server, agent and period (the search text does not reach the
    /// export), written to a private folder of the temporary directory. Needs a server.
    func export(_ format: ExportFormat) async throws -> URL {
        guard let id = filter.serverID, let server = state.servers.first(where: { $0.id == id }) else {
            throw ExportError.chooseServer
        }
        let file: HistoryExport
        do {
            file = try await state.api(for: server).export(format, agent: filter.agentID, since: filter.since, until: filter.until)
        } catch {
            throw ExportError.failed(APIError.text(error))
        }
        discardExport()
        let folder = FileManager.default.temporaryDirectory.appending(path: "wristcall-export-\(UUID().uuidString)")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            exportDirectory = folder
            // The name was cleaned by the client; keep only its last component all the same.
            var name = URL(fileURLWithPath: file.filename).lastPathComponent
            if name.isEmpty || name == "." || name == ".." { name = "wristcall-history.\(format.rawValue)" }
            let url = folder.appending(path: name)
            try file.data.write(to: url, options: [.atomic, .completeFileProtection])
            return url
        } catch {
            discardExport()
            throw ExportError.failed("The file could not be saved.")
        }
    }

    /// Removes the exported file: it holds what was said in the calls.
    func discardExport() {
        if let exportDirectory { try? FileManager.default.removeItem(at: exportDirectory) }
        exportDirectory = nil
    }

    // MARK: Plumbing

    private func servers(for filter: Filter) -> [ManagedServer] {
        state.servers.filter { filter.serverID == nil || $0.id == filter.serverID }
    }

    private func api(for item: Item) -> (any ServerAPI)? {
        guard let server = state.servers.first(where: { $0.id == item.serverID }) else {
            error = "That server was removed."
            return nil
        }
        return state.api(for: server)
    }

    private func query(_ filter: Filter, before: String?) -> HistoryQuery {
        HistoryQuery(
            agent: filter.serverID == nil ? nil : filter.agentID, text: filter.text,
            since: filter.since, until: filter.until, before: before, limit: pageSize
        )
    }

    private func replace(_ item: Item, with call: CallRecord) {
        guard let index = records[item.serverID]?.firstIndex(where: { $0.id == item.call.id }) else { return }
        records[item.serverID]?[index] = call
        rebuild()
    }

    private func rebuild() {
        var all: [Item] = []
        for (serverID, calls) in records {
            let name = names[serverID] ?? serverID
            all += calls.map { Item(serverID: serverID, serverName: name, call: $0) }
        }
        all.sort { left, right in
            if left.call.createdAt != right.call.createdAt { return left.call.createdAt > right.call.createdAt }
            if left.call.id != right.call.id { return left.call.id > right.call.id }
            return left.serverID < right.serverID
        }
        items = all
        canLoadMore = !cursors.isEmpty
    }

    private nonisolated static func fetch(
        _ jobs: [(id: String, api: any ServerAPI, query: HistoryQuery)]
    ) async -> [String: Result<CallPage, any Error>] {
        await withTaskGroup(of: (String, Result<CallPage, any Error>).self) { group in
            for job in jobs {
                group.addTask {
                    do { return (job.id, .success(try await job.api.calls(job.query))) } catch { return (job.id, .failure(error)) }
                }
            }
            var all: [String: Result<CallPage, any Error>] = [:]
            for await (id, result) in group { all[id] = result }
            return all
        }
    }
}

extension CallRecord {
    /// What was said, one line per utterance; a one-way call without entries gives its text.
    var transcript: String {
        let lines = entries.compactMap { entry -> String? in
            let who = entry.role == "user" ? "You" : "Agent"
            if let text = entry.text, !text.isEmpty { return "\(who): \(text)" }
            if let error = entry.error { return "\(who): (\(error))" }
            return nil
        }
        return lines.isEmpty ? (text ?? "") : lines.joined(separator: "\n")
    }
}
