import Foundation
import Observation
import WristcallKit

/// The agents of every saved server and of the Wristcall Cloud entry, one section each (the Agents home). A server
/// that does not answer keeps its section (with what was on screen and the reason); the others are not affected.
/// The Cloud entry's section comes first; when its node does not answer, it always shows the agents of the agenda.
@MainActor
@Observable
final class AllAgentsModel {
    struct Section: Identifiable, Equatable {
        var server: ManagedServer
        var agents: [AgentDetail]
        var error: String?

        var id: String { server.id }
    }

    /// In the order of `AppState.allServers`: the Cloud entry first.
    private(set) var sections: [Section] = []
    /// `true` once the servers were asked at least once (before it, empty sections mean "not known yet").
    private(set) var didLoad = false

    @ObservationIgnored private let servers: @MainActor () -> [ManagedServer]
    @ObservationIgnored private let makeAPI: @MainActor (ManagedServer) -> any ServerAPI
    @ObservationIgnored private let agendaAgents: @MainActor (ManagedServer) -> [AgentDetail]

    /// `agenda`: what the Cloud's agenda lists for a server (the Cloud entry's agents), shown when it fails.
    init(
        servers: @escaping @MainActor () -> [ManagedServer],
        api: @escaping @MainActor (ManagedServer) -> any ServerAPI,
        agenda: @escaping @MainActor (ManagedServer) -> [AgentDetail] = { _ in [] }
    ) {
        self.servers = servers
        makeAPI = api
        agendaAgents = agenda
    }

    convenience init(state: AppState) {
        self.init(servers: { state.allServers }, api: { state.api(for: $0) }, agenda: { server in
            guard server.isCloudEntry, let entry = state.cloudEntry else { return [] }
            return entry.agents.map(AgentDetail.init(cloud:))
        })
    }

    /// Asks every server for its agents, in parallel.
    func load() async {
        // Stable: the Cloud entry ahead, the others in their order.
        let all = servers()
        let current = all.filter(\.isCloudEntry) + all.filter { !$0.isCloudEntry }
        let previous = Dictionary(sections.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        let clients = current.map { ($0.id, makeAPI($0)) }
        let results = await withTaskGroup(of: (String, Result<[AgentDetail], any Error>).self) { group in
            for (id, client) in clients {
                group.addTask { (id, await Self.agents(of: client)) }
            }
            var all: [String: Result<[AgentDetail], any Error>] = [:]
            for await (id, result) in group { all[id] = result }
            return all
        }
        // The screen left (or the servers changed and the load starts over): nothing to say.
        guard !Task.isCancelled else { return }
        sections = current.map { server in
            section(server, result: results[server.id], previous: previous[server.id])
        }
        didLoad = true
    }

    /// Asks one server again (after the form saved or deleted something there).
    func reload(server: ManagedServer) async {
        guard let index = sections.firstIndex(where: { $0.id == server.id }) else {
            await load()
            return
        }
        let result = await Self.agents(of: makeAPI(server))
        guard !Task.isCancelled, let at = sections.firstIndex(where: { $0.id == server.id }) else { return }
        sections[at] = section(server, result: result, previous: sections[index])
    }

    /// The Siri line of the footer names the first agent on the screen.
    var firstAgentName: String? {
        sections.lazy.flatMap(\.agents).first?.displayName
    }

    // MARK: - Private

    private static func agents(of api: any ServerAPI) async -> Result<[AgentDetail], any Error> {
        do {
            return .success(try await api.agents())
        } catch {
            return .failure(error)
        }
    }

    private func section(
        _ server: ManagedServer, result: Result<[AgentDetail], any Error>?, previous: Section?
    ) -> Section {
        switch result {
        case .success(let list):
            // Server order: `position`, then the order it answered in.
            let ordered = list.enumerated().sorted { lhs, rhs in
                (lhs.element.position, lhs.offset) < (rhs.element.position, rhs.offset)
            }.map(\.element)
            return Section(server: server, agents: ordered, error: nil)
        case .failure(let error):
            // The Cloud entry always shows what the agenda lists; the others keep what was on screen.
            let agents = server.isCloudEntry ? agendaAgents(server) : (previous?.agents ?? [])
            return Section(server: server, agents: agents, error: AgentsModel.message(error))
        case nil:
            return Section(server: server, agents: previous?.agents ?? [], error: nil)
        }
    }
}

/// What a row says about an agent.
enum AgentSummary {
    static func callTypeLabel(_ type: String) -> String {
        switch type {
        case "conversation": "Conversation"
        case "one-shot": "One-shot"
        case "monologue": "Monologue"
        default: type
        }
    }

    /// "Conversation · api.openai.com": the host the agent's action calls (a custom `openai_chat` endpoint or a
    /// webhook), "your server" when the server's own provider answers ("Wristcall Cloud" for the Cloud entry's agents).
    static func subtitle(_ agent: AgentDetail, cloud: Bool = false) -> String {
        "\(callTypeLabel(agent.callType)) · \(host(of: agent.action) ?? (cloud ? "Wristcall Cloud" : "your server"))"
    }

    static func host(of action: JSONValue?) -> String? {
        guard case .object(let object)? = action, object["provider"] == nil else { return nil }
        let text = object["base_url"]?.stringValue ?? object["url"]?.stringValue
        guard let text, let host = URL(string: text)?.host(), !host.isEmpty else { return nil }
        return host
    }

    /// The text under the list: how to edit, and (with an agent to name) the Siri phrase.
    static func footer(firstAgent: String?) -> String? {
        guard let firstAgent else { return nil }
        return "Tap an agent to edit it. Call agents from your watch, or ask Siri: \u{201C}Call \(firstAgent) with wristcall\u{201D}."
    }
}

/// What the Agents home shows around the sections: the empty state, "New agent", and the Wristcall Cloud line when
/// there is no Cloud entry to show (yet).
struct AgentsHomeLayout: Equatable {
    enum CloudNotice: Equatable {
        /// Nothing: the Cloud entry has its own section, or there is no plan and the list has servers.
        case none
        /// A plan, but the Cloud has not added its entry to the agenda yet.
        case settingUp
        /// No plan and nothing to show: "Try Wristcall Cloud".
        case tryCloud
    }

    static let cloudHeader = "WRISTCALL CLOUD · BETA"
    static let settingUpText = "Setting up Wristcall Cloud…"

    /// No server at all, the Cloud entry included: "Connect your server".
    let isEmpty: Bool
    let canCreateAgent: Bool
    let cloudNotice: CloudNotice

    /// `servers`: `AppState.allServers`. A lapsed plan with the entry still there shows it like any other.
    init(servers: [ManagedServer], hasPlan: Bool) {
        isEmpty = servers.isEmpty
        canCreateAgent = !servers.isEmpty
        cloudNotice = if servers.contains(where: \.isCloudEntry) {
            .none
        } else if hasPlan {
            .settingUp
        } else {
            servers.isEmpty ? .tryCloud : .none
        }
    }

    static func header(for server: ManagedServer) -> String {
        server.isCloudEntry ? cloudHeader : server.name.uppercased()
    }

    /// "+" (D12): the first own server that answered, else the first own server; the Cloud entry only without one.
    static func newAgentServer(
        sections: [AllAgentsModel.Section], own: [ManagedServer], cloud: ManagedServer?
    ) -> ManagedServer? {
        sections.first { !$0.server.isCloudEntry && $0.error == nil }?.server ?? own.first ?? cloud
    }
}
