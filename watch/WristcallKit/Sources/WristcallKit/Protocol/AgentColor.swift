import Foundation

/// The accent color of an agent (`color` in the agent summary, detail and Cloud agenda). The apps draw it from
/// their own palette: only the name travels.
public enum AgentColor: String, Sendable, Codable, CaseIterable {
    case red, orange, yellow, green, mint, teal, blue, indigo, purple, pink

    /// What an agent without a (known) color wears.
    public static let fallback: AgentColor = .mint

    public static func resolved(_ color: AgentColor?) -> AgentColor {
        color ?? fallback
    }
}

extension KeyedDecodingContainer {
    /// A color the client does not know (a newer server), `null` or a missing key is `nil`, never an error:
    /// it must not drop the agent.
    func decodeAgentColor(forKey key: Key) -> AgentColor? {
        guard let name = (try? decodeIfPresent(String.self, forKey: key)) ?? nil else { return nil }
        return AgentColor(rawValue: name)
    }
}
