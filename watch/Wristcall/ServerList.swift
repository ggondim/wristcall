import Foundation
import WristcallKit

/// Where one server's `GET /v1/me` stands. Each server has its own, so one that is down does not
/// hide the agents of the others (decision W3).
enum ServerStatus: Equatable {
    case loading
    case ready(DeviceInfo)
    /// `GET /v1/me` failed; the text says why. "Retry" asks again.
    case unavailable(String)
}

/// One paired server and what the watch knows about it right now.
struct ServerEntry: Identifiable, Equatable {
    var credentials: Credentials
    var status: ServerStatus

    /// The local id (`Credentials.id`), not the server's: it names the server in `AgentRef`s.
    var id: String { credentials.id }
    /// What the screens call the server: the host, with the port when there is one (two local
    /// servers differ only by it).
    var host: String { credentials.serverURL.displayHost }

    /// `GET /v1/me` failed: the row offers "Retry".
    var isUnavailable: Bool {
        if case .unavailable = status { true } else { false }
    }

    /// The agents this server listed, in its order; none until it answers.
    var agents: [AgentTarget] {
        guard case .ready(let info) = status else { return [] }
        return info.agents.map { AgentTarget(serverID: id, serverHost: host, agent: $0) }
    }
}

/// One agent of one server: what a tap on the grid, a shortcut or a complication calls.
struct AgentTarget: Identifiable, Hashable {
    let serverID: String
    let serverHost: String
    let agent: Agent

    var ref: AgentRef { AgentRef(serverID: serverID, agentID: agent.id) }
    var id: String { ref.description }

    /// What the widget extension and App Intents may know about it: no token, no URL.
    var catalogEntry: CatalogAgent {
        CatalogAgent(
            ref: ref, slug: agent.slug, displayName: agent.displayName, icon: agent.icon,
            callType: agent.callType.wireValue, serverHost: serverHost)
    }
}

extension URL {
    /// `host` or `host:port`; the whole address when there is no host.
    var displayHost: String {
        guard let host = host() else { return absoluteString }
        return port.map { "\(host):\($0)" } ?? host
    }
}
