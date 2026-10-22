import Foundation

/// How an agent takes a call (`call_type` in the agent summary).
public enum CallType: OpenWireValue, Sendable, Hashable {
    /// The user talks and the agent answers with its voice.
    case conversation
    /// Only listens: records until the user hangs up (or the agent's turn limit), then delivers once.
    case oneShot
    /// Only listens, for as long as the user talks (up to the server's one-way limit).
    case monologue
    /// A type a newer server added; the watch must not offer it.
    case unknown(String)

    public init(wireValue: String) {
        switch wireValue {
        case "conversation": self = .conversation
        case "one-shot": self = .oneShot
        case "monologue": self = .monologue
        default: self = .unknown(wireValue)
        }
    }

    public var wireValue: String {
        switch self {
        case .conversation: "conversation"
        case .oneShot: "one-shot"
        case .monologue: "monologue"
        case .unknown(let value): value
        }
    }

    /// One-shot and monologue: no answer comes back, the result is read later by call id.
    public var isOneWay: Bool {
        switch self {
        case .oneShot, .monologue: true
        case .conversation, .unknown: false
        }
    }

    /// `false` only for a type this client does not know.
    public var isSupported: Bool {
        if case .unknown = self { false } else { true }
    }
}

/// An agent as listed by `GET /v1/me` (and echoed in `session.ready`): what a call talks to.
public struct Agent: Decodable, Sendable, Hashable, Identifiable {
    /// Stable id (`ag_` + 12 hex); for an agent made from a 0.2.x profile, the profile name.
    public var id: String
    /// Short name, unique per user; the 0.2.x servers know agents only by it.
    public var slug: String
    public var displayName: String
    /// An SF Symbol name.
    public var icon: String
    public var callType: CallType
    /// The mode `session.start` gets when the user does not pick one.
    public var turnEnd: TurnEnd
    /// The accent color; `nil` when the server sent none (or one this client does not know).
    public var color: AgentColor?

    public static let defaultIcon = "waveform"

    public init(
        id: String,
        slug: String,
        displayName: String,
        icon: String = Agent.defaultIcon,
        callType: CallType = .conversation,
        turnEnd: TurnEnd = .auto,
        color: AgentColor? = nil
    ) {
        self.id = id
        self.slug = slug
        self.displayName = displayName
        self.icon = icon
        self.callType = callType
        self.turnEnd = turnEnd
        self.color = color
    }

    /// A 0.2.x server has only profiles: each one is a plain conversation agent (id = slug = name).
    public init(profile: Profile) {
        self.init(id: profile.name, slug: profile.name, displayName: profile.displayName)
    }

    /// An agent of the Cloud agenda (it has no turn end, so `.auto`).
    public init(cloud agent: CloudAgent) {
        self.init(
            id: agent.id, slug: agent.slug, displayName: agent.displayName, icon: agent.icon,
            callType: CallType(wireValue: agent.callType), color: agent.color)
    }

    private enum CodingKeys: String, CodingKey {
        case id, slug, icon, color
        case displayName = "display_name"
        case callType = "call_type"
        case turnEnd = "turn_end"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        slug = try container.decode(String.self, forKey: .slug)
        displayName = try container.decode(String.self, forKey: .displayName)
        icon = try container.decodeIfPresent(String.self, forKey: .icon) ?? Agent.defaultIcon
        callType = try container.decodeIfPresent(CallType.self, forKey: .callType) ?? .conversation
        // A mode this client does not know is the server's default, not a reason to drop the agent.
        turnEnd = try container.decodeIfPresent(String.self, forKey: .turnEnd).flatMap(TurnEnd.init(rawValue:)) ?? .auto
        color = container.decodeAgentColor(forKey: .color)
    }
}

/// The account behind a device, as listed by `GET /v1/me` since server 0.5.0.
public struct UserInfo: Decodable, Sendable, Equatable {
    public var id: String
    public var handle: String
    public var displayName: String?

    public init(id: String, handle: String, displayName: String? = nil) {
        self.id = id
        self.handle = handle
        self.displayName = displayName
    }

    private enum CodingKeys: String, CodingKey {
        case id, handle
        case displayName = "display_name"
    }
}
