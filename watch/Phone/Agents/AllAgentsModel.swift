import Foundation
import Observation
import WristcallKit

/// The agents of every saved server, one section each (the Agents home). A server that does not answer keeps
/// its section (with what was on screen and the reason); the others are not affected.
@MainActor
@Observable
final class AllAgentsModel {
    struct Section: Identifiable, Equatable {
        var server: ManagedServer
        var agents: [AgentDetail]
        var error: String?

        var id: String { server.id }
    }

    /// In the order of `AppState.servers`.
    private(set) var sections: [Section] = []
    /// `true` once the servers were asked at least once (before it, empty sections mean "not known yet").
    private(set) var didLoad = false

    @ObservationIgnored private let servers: @MainActor () -> [ManagedServer]
    @ObservationIgnored private let makeAPI: @MainActor (ManagedServer) -> any ServerAPI

    init(
        servers: @escaping @MainActor () -> [ManagedServer],
        api: @escaping @MainActor (ManagedServer) -> any ServerAPI
    ) {
        self.servers = servers
        makeAPI = api
    }

    convenience init(state: AppState) {
        self.init(servers: { state.servers }, api: { state.api(for: $0) })
    }

    /// Asks every server for its agents, in parallel.
    func load() async {
        let current = servers()
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
            Self.section(server, result: results[server.id], previous: previous[server.id])
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
        sections[at] = Self.section(server, result: result, previous: sections[index])
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

    private static func section(
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
            return Section(server: server, agents: previous?.agents ?? [], error: AgentsModel.message(error))
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
    /// webhook), "your server" when the server's own provider answers.
    static func subtitle(_ agent: AgentDetail) -> String {
        "\(callTypeLabel(agent.callType)) · \(host(of: agent.action) ?? "your server")"
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
