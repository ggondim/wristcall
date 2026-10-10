import Foundation
import Observation
import WristcallKit

/// The agents of one server: what the list screen shows and what it changes.
@MainActor
@Observable
final class AgentsModel {
    private(set) var agents: [AgentDetail] = []
    /// `nil` until the server has answered `GET /v1/providers` (the form needs it).
    private(set) var providers: ProviderList?
    private(set) var isLoading = false
    var error: String?

    let server: ManagedServer
    @ObservationIgnored private let api: any ServerAPI
    /// Called with the whole list after every change (the agenda sync hangs here).
    @ObservationIgnored private let onChange: (@MainActor ([AgentDetail]) async -> Void)?

    init(server: ManagedServer, api: any ServerAPI, onChange: (@MainActor ([AgentDetail]) async -> Void)? = nil) {
        self.server = server
        self.api = api
        self.onChange = onChange
    }

    /// Reads agents (in server order) and providers. A failure keeps what was on screen and sets `error`.
    func load() async {
        isLoading = true
        defer { isLoading = false }
        do {
            let list = try await api.agents()
            agents = list.enumerated().sorted { lhs, rhs in
                (lhs.element.position, lhs.offset) < (rhs.element.position, rhs.offset)
            }.map(\.element)
        } catch {
            self.error = Self.message(error)
            return
        }
        do {
            providers = try await api.providers()
            error = nil
        } catch {
            self.error = Self.message(error)
        }
        await onChange?(agents)
    }

    func delete(_ agent: AgentDetail) async {
        do {
            try await api.deleteAgent(agent.id)
        } catch {
            self.error = Self.message(error)
            return
        }
        error = nil
        agents.removeAll { $0.id == agent.id }
        await onChange?(agents)
    }

    /// `List.onMove`: `to` is the gap the row is dropped in, counted before the row leaves its place, so
    /// moving down shifts the final index by one. The server takes the final index as `position`.
    func move(from: IndexSet, to: Int) async {
        guard let source = from.first, agents.indices.contains(source) else { return }
        let destination = to > source ? to - 1 : to
        guard destination != source, agents.indices.contains(destination) else { return }
        let moved = agents.remove(at: source)
        agents.insert(moved, at: destination)
        do {
            _ = try await api.updateAgent(moved.id, ["position": .int(destination)])
            error = nil
            await load()
        } catch {
            let text = Self.message(error)
            await load()
            self.error = text
        }
    }

    /// Creates (`editing == nil`) or updates an agent. Returns the text to show when the server refuses.
    func save(_ fields: AgentFields, editing: AgentDetail?) async -> String? {
        do {
            if let editing {
                if !fields.isEmpty { _ = try await api.updateAgent(editing.id, fields) }
            } else {
                _ = try await api.createAgent(fields)
            }
        } catch {
            return Self.message(error)
        }
        await load()
        return nil
    }

    static func message(_ error: any Error) -> String {
        (error as? APIError)?.message ?? "Can't reach the server."
    }
}
