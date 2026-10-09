import Foundation

/// Names one agent of one paired server: the local server id (`Credentials.id`) and the agent's
/// own id. The text form `"<serverID>/<agentID>"` travels in URLs, App Intents and the catalog.
/// The id is used, not the slug, because the slug can change and the id cannot.
public struct AgentRef: Hashable, Sendable, Codable, LosslessStringConvertible {
    public let serverID: String
    public let agentID: String

    public init(serverID: String, agentID: String) {
        self.serverID = serverID
        self.agentID = agentID
    }

    /// `nil` unless the text is exactly two non-empty parts around one `/`.
    public init?(_ description: String) {
        let parts = description.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
        self.init(serverID: String(parts[0]), agentID: String(parts[1]))
    }

    public var description: String { "\(serverID)/\(agentID)" }
}
