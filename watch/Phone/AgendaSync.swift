import Foundation
import Observation
import WristcallKit

/// Mirrors the servers of this iPhone (name, canonical URL, `linked`) and their agents to the account's
/// agenda in wristcall Cloud, which the watch reads when it signs in with the account and other apps show.
///
/// Best effort and in the background: every change only queues a job and returns at once, so a slow or
/// hung Cloud never holds up managing servers or agents. Jobs run one after another; each reads the latest
/// state when it runs (several changes of one server's agents send only the last list). An error only sets
/// `notice`. `invalidate()` (sign out, account deletion) drops the queue: a job still running checks the
/// generation before every Cloud write, so nothing is written back after it.
@MainActor
@Observable
final class AgendaSync {
    /// The Cloud keeps at most this many agents per server.
    static let agentLimit = 50
    /// The Cloud's limit for names (Unicode scalars).
    static let textLimit = 64

    /// Last failure, for the screen; cleared by the next success.
    private(set) var notice: String?
    /// The session ended (refresh token refused) while syncing.
    @ObservationIgnored var onSignedOut: (@MainActor () async -> Void)?
    /// Bumped by `invalidate()`; a job of an older generation writes nothing.
    @ObservationIgnored private(set) var generation = 0

    @ObservationIgnored private let session: AccountSession
    @ObservationIgnored private let state: AppState
    /// The last job queued; each job waits for the one before it.
    @ObservationIgnored private var chain: Task<Void, Never>?
    /// Agents lists not sent yet, by local server id: the job takes the latest one when it gets to it.
    @ObservationIgnored private var pendingAgents: [String: [AgentDetail]] = [:]
    /// Servers with an add/update job queued and not started yet.
    @ObservationIgnored private var queuedServers: Set<String> = []
    @ObservationIgnored private var pushAllQueued = false
    /// While set (a deletion or sign out on its way), changes queue nothing.
    @ObservationIgnored private var suspended = false

    init(session: AccountSession, state: AppState) {
        self.session = session
        self.state = state
    }

    private var cloud: CloudClient { session.cloudClient }

    // MARK: - Changes (queue a job, return at once)

    /// Adds or updates every server, keeps their agenda ids, then sends each one's agents (servers that do
    /// not answer keep the agents the agenda has).
    func pushAll() {
        guard !suspended, !pushAllQueued else { return }
        pushAllQueued = true
        enqueue { generation in
            self.pushAllQueued = false
            var failed = false
            for server in self.state.servers {
                switch await self.attempt(generation, {
                    guard let id = try await self.upsert(server.id, generation) else { return }
                    guard let agents = try? await self.state.api(for: server).agents() else { return }
                    try await self.sendAgents(agents, serverID: id, generation)
                }) {
                case .done: break
                case .failed: failed = true
                case .stale: return
                case .signedOut:
                    await self.signedOut()
                    return
                }
            }
            self.notice = failed ? Self.writeNotice : nil
        }
    }

    func serverAdded(_ server: ManagedServer) {
        guard !suspended, !queuedServers.contains(server.id) else { return }
        queuedServers.insert(server.id)
        enqueue { generation in
            self.queuedServers.remove(server.id)
            await self.run(generation, failure: Self.writeNotice) { _ = try await self.upsert(server.id, generation) }
        }
    }

    /// A rename or a link: same as an add (update, or add when the agenda lost it).
    func serverChanged(_ server: ManagedServer) {
        serverAdded(server)
    }

    /// Deletes the entry (already gone counts as done). Without a known id, the entry is found by its URL.
    func serverRemoved(_ server: ManagedServer) {
        pendingAgents[server.id] = nil
        guard !suspended else { return }
        enqueue { generation in
            await self.run(generation, failure: Self.writeNotice) {
                let token = try await self.token()
                var id = server.cloudServerID
                if id == nil {
                    let canonical = ServerAddress.canonical(server.url)
                    id = try await self.cloud.servers(accessToken: token).first { Self.canonical($0.url) == canonical }?.id
                }
                guard let id else { return }
                try self.check(generation)
                try await self.cloud.deleteServer(id: id, accessToken: token)
            }
        }
    }

    /// `PUT` of the agents' summary, the first 50 in the server's order. Only the latest list is sent.
    func agentsChanged(_ server: ManagedServer, _ agents: [AgentDetail]) {
        guard !suspended else { return }
        let queued = pendingAgents[server.id] != nil
        pendingAgents[server.id] = agents
        guard !queued else { return }
        enqueue { generation in
            var took = false
            // Already sent by an earlier job (it takes the latest list): nothing to write.
            guard self.pendingAgents[server.id] != nil else { return }
            await self.run(generation, failure: Self.writeNotice) {
                guard let id = try await self.upsert(server.id, generation) else { return }
                guard let latest = self.pendingAgents.removeValue(forKey: server.id) else { return }
                took = true
                try await self.sendAgents(latest, serverID: id, generation)
            }
            // Failed before sending: drop it (the next load sends the list again), so later changes queue anew.
            if !took, generation == self.generation { self.pendingAgents[server.id] = nil }
        }
    }

    /// Drops every queued job and stops the running one from writing (sign out, account deleted).
    func invalidate() {
        generation += 1
        chain?.cancel()
        chain = nil
        pendingAgents = [:]
        queuedServers = []
        pushAllQueued = false
        notice = nil
    }

    /// `invalidate()`, and nothing new is queued until `resume()`: for the time a deletion or sign out takes.
    func suspend() {
        invalidate()
        suspended = true
    }

    func resume() {
        suspended = false
    }

    /// Waits until the queue is empty (tests, and callers that need the agenda written).
    func idle() async {
        while let task = chain {
            await task.value
            if chain == task {
                chain = nil
                return
            }
        }
    }

    // MARK: - Reading

    /// Agenda servers with no token on this iPhone (compared by canonical URL), to set up here. Entries whose
    /// URL the app would not use (`http://` off localhost: the Cloud accepts any) are left out.
    func pendingSetup() async -> [CloudServer] {
        guard await session.isSignedIn else { return [] }
        let local = Set(state.servers.map { ServerAddress.canonical($0.url) })
        do {
            let entries = try await cloud.servers(accessToken: try await token())
            notice = nil
            return entries.filter { entry in
                guard let url = ServerAddress.parse(entry.url) else { return false }
                return !local.contains(ServerAddress.canonical(url))
            }
        } catch AccountError.signedOut {
            await signedOut()
        } catch {
            notice = "Can't read your servers from wristcall Cloud."
        }
        return []
    }

    // MARK: - Plumbing

    private static let writeNotice = "Can't update your servers in wristcall Cloud."

    private func enqueue(_ work: @escaping @MainActor (Int) async -> Void) {
        let generation = generation
        let previous = chain
        chain = Task { @MainActor in
            await previous?.value
            // A stale job still runs: it only clears its own bookkeeping, `check` stops every write.
            await work(generation)
        }
    }

    /// The agenda id of the server `id` as it is now (`nil` when it was removed meanwhile): `PATCH` of the
    /// known entry, or `POST` (a `409` returns the existing one). The id is kept in the app's list.
    private func upsert(_ id: String, _ generation: Int) async throws -> String? {
        guard let server = state.servers.first(where: { $0.id == id }) else { return nil }
        let token = try await token()
        let name = Self.cut(server.name, or: server.url.host() ?? "Server")
        if let cloudID = server.cloudServerID {
            do {
                try check(generation)
                _ = try await cloud.updateServer(id: cloudID, name: name, linked: server.linked, accessToken: token)
                return cloudID
            } catch APIError.notFound {
                // Deleted from the agenda elsewhere: add it again below.
            }
        }
        try check(generation)
        let entry = try await cloud.addServer(name: name, url: server.url, linked: server.linked, accessToken: token)
        if entry.linked != server.linked || entry.name != name {
            try check(generation)
            _ = try await cloud.updateServer(id: entry.id, name: name, linked: server.linked, accessToken: token)
        }
        try check(generation)
        state.setCloudServerID(entry.id, for: server.id)
        return entry.id
    }

    private func sendAgents(_ agents: [AgentDetail], serverID: String, _ generation: Int) async throws {
        let token = try await token()
        try check(generation)
        try await cloud.setAgents(Self.snapshot(agents), serverID: serverID, accessToken: token)
    }

    private func token() async throws -> String { try await session.accessToken() }

    /// A job of an older generation (signed out or account deleted meanwhile).
    private struct Stale: Error {}

    private func check(_ generation: Int) throws {
        guard generation == self.generation, !Task.isCancelled else { throw Stale() }
    }

    private enum Outcome { case done, failed, signedOut, stale }

    private func attempt(_ generation: Int, _ work: () async throws -> Void) async -> Outcome {
        guard generation == self.generation else { return .stale }
        do {
            try await work()
            return generation == self.generation ? .done : .stale
        } catch {
            // A stale job's failure (often the cancelled request) is nobody's business.
            if generation != self.generation || error is Stale { return .stale }
            if case AccountError.signedOut? = error as? AccountError { return .signedOut }
            // The error itself is not kept: it says nothing the user can act on.
            return .failed
        }
    }

    private func run(_ generation: Int, failure: String, _ work: () async throws -> Void) async {
        switch await attempt(generation, work) {
        case .done: notice = nil
        case .failed: notice = failure
        case .stale: break
        case .signedOut: await signedOut()
        }
    }

    private func signedOut() async {
        notice = nil
        await onSignedOut?()
    }

    private static func canonical(_ text: String) -> String? {
        URL(string: text).map(ServerAddress.canonical)
    }

    static func snapshot(_ agents: [AgentDetail]) -> [CloudAgent] {
        agents.prefix(agentLimit).map {
            CloudAgent(id: $0.id, slug: $0.slug, displayName: cut($0.displayName, or: $0.slug), icon: $0.icon,
                       callType: $0.callType)
        }
    }

    /// At most 64 Unicode scalars (what the Cloud counts), without splitting a character; `fallback` (cut too)
    /// when nothing is left.
    static func cut(_ text: String, or fallback: String) -> String {
        let result = cut(text)
        return result.trimmingCharacters(in: .whitespaces).isEmpty ? cut(fallback) : result
    }

    private static func cut(_ text: String) -> String {
        var result = ""
        var count = 0
        for character in text {
            let size = character.unicodeScalars.count
            guard count + size <= textLimit else { break }
            result.append(character)
            count += size
        }
        return result
    }
}
