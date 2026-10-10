import Foundation
import Observation
import WristcallKit

/// Mirrors the servers of this iPhone (name, canonical URL, `linked`) and their agents to the account's
/// agenda in wristcall Cloud, which the watch reads when it signs in with the account and other apps show.
/// Best effort: an error only sets `notice`, never stops managing servers. Signed out, it does nothing.
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

    @ObservationIgnored private let session: AccountSession
    @ObservationIgnored private let state: AppState

    init(session: AccountSession, state: AppState) {
        self.session = session
        self.state = state
    }

    private var cloud: CloudClient { session.cloudClient }

    /// Adds or updates every server, keeps their agenda ids, then sends each one's agents (servers that do
    /// not answer keep the agents the agenda has).
    func pushAll() async {
        guard await session.isSignedIn else { return }
        var failed = false
        for server in state.servers {
            switch await attempt({
                let entry = try await self.upsert(server)
                guard let agents = try? await self.state.api(for: server).agents() else { return }
                try await self.cloud.setAgents(Self.snapshot(agents), serverID: entry, accessToken: try await self.token())
            }) {
            case .done: break
            case .failed: failed = true
            case .signedOut:
                await signedOut()
                return
            }
        }
        notice = failed ? Self.writeNotice : nil
    }

    func serverAdded(_ server: ManagedServer) async {
        guard await session.isSignedIn else { return }
        await run(failure: Self.writeNotice) { _ = try await self.upsert(server) }
    }

    /// A rename or a link: same as an add (update, or add when the agenda lost it).
    func serverChanged(_ server: ManagedServer) async {
        await serverAdded(server)
    }

    /// Deletes the entry (already gone counts as done). Without a known id, the entry is found by its URL.
    func serverRemoved(_ server: ManagedServer) async {
        guard await session.isSignedIn else { return }
        await run(failure: Self.writeNotice) {
            let token = try await self.token()
            var id = server.cloudServerID
            if id == nil {
                let canonical = ServerAddress.canonical(server.url)
                id = try await self.cloud.servers(accessToken: token).first { Self.canonical($0.url) == canonical }?.id
            }
            guard let id else { return }
            try await self.cloud.deleteServer(id: id, accessToken: token)
        }
    }

    /// `PUT` of the agents' summary, the first 50 in the server's order.
    func agentsChanged(_ server: ManagedServer, _ agents: [AgentDetail]) async {
        guard await session.isSignedIn else { return }
        await run(failure: Self.writeNotice) {
            let current = self.state.servers.first { $0.id == server.id } ?? server
            let id = try await self.upsert(current)
            try await self.cloud.setAgents(Self.snapshot(agents), serverID: id, accessToken: try await self.token())
        }
    }

    /// Agenda servers with no token on this iPhone (compared by canonical URL), to set up here. Entries whose
    /// URL the app would not use (`http://` off localhost: the Cloud accepts any) are left out.
    func pendingSetup() async -> [CloudServer] {
        guard await session.isSignedIn else { return [] }
        let local = Set(state.servers.map { ServerAddress.canonical($0.url) })
        var pending: [CloudServer] = []
        await run(failure: "Can't read your servers from wristcall Cloud.") {
            pending = try await self.cloud.servers(accessToken: try await self.token()).filter { entry in
                guard let url = ServerAddress.parse(entry.url) else { return false }
                return !local.contains(ServerAddress.canonical(url))
            }
        }
        return pending
    }

    // MARK: - Plumbing

    private static let writeNotice = "Can't update your servers in wristcall Cloud."

    /// The agenda id of `server`: `PATCH` of the known entry, or `POST` (a `409` returns the existing one).
    /// The id is kept in the app's list.
    private func upsert(_ server: ManagedServer) async throws -> String {
        let token = try await token()
        let name = Self.cut(server.name, or: server.url.host() ?? "Server")
        if let id = server.cloudServerID {
            do {
                _ = try await cloud.updateServer(id: id, name: name, linked: server.linked, accessToken: token)
                return id
            } catch APIError.notFound {
                // Deleted from the agenda elsewhere: add it again below.
            }
        }
        let entry = try await cloud.addServer(name: name, url: server.url, linked: server.linked, accessToken: token)
        if entry.linked != server.linked || entry.name != name {
            _ = try await cloud.updateServer(id: entry.id, name: name, linked: server.linked, accessToken: token)
        }
        if state.servers.contains(where: { $0.id == server.id }) {
            state.setCloudServerID(entry.id, for: server.id)
        }
        return entry.id
    }

    private func token() async throws -> String { try await session.accessToken() }

    private enum Outcome { case done, failed, signedOut }

    private func attempt(_ work: () async throws -> Void) async -> Outcome {
        do {
            try await work()
            return .done
        } catch AccountError.signedOut {
            return .signedOut
        } catch {
            // The error itself is not kept: it says nothing the user can act on.
            return .failed
        }
    }

    private func run(failure: String, _ work: () async throws -> Void) async {
        switch await attempt(work) {
        case .done: notice = nil
        case .failed: notice = failure
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
