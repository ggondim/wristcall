import Foundation

/// `GET {cloud}/v1/config`: the issuer, the OIDC clients and scopes the apps sign in with, and what the Cloud offers.
public struct CloudConfig: Decodable, Sendable, Equatable {
    public var issuer: URL
    public var projectId: String?
    public var clients: Clients
    public var scopes: [String]
    /// Whether the Cloud issues per-server tokens (`POST /v1/server-tokens`).
    public var serverTokens: Bool
    public var push: Push
    /// The Cloud session routes (`/v1/sessions`); `nil` on a Cloud that predates them.
    public var sessions: SessionsConfig?

    /// The client id of each app; `nil` when the Cloud has none for it.
    public struct Clients: Decodable, Sendable, Equatable {
        public var ios: String?
        public var pwa: String?
        public var watch: String?

        public init(ios: String?, pwa: String?, watch: String?) {
            self.ios = ios
            self.pwa = pwa
            self.watch = watch
        }
    }

    public struct Push: Decodable, Sendable, Equatable {
        public var apns: Bool
        public var webpush: Bool
        public var apnsTopics: [String]

        public init(apns: Bool, webpush: Bool, apnsTopics: [String]) {
            self.apns = apns
            self.webpush = webpush
            self.apnsTopics = apnsTopics
        }

        private enum CodingKeys: String, CodingKey {
            case apns, webpush
            case apnsTopics = "apns_topics"
        }
    }

    public struct SessionsConfig: Decodable, Sendable, Equatable {
        public var enabled: Bool
        /// How recent a Zitadel login must be for sensitive session actions, in seconds.
        public var zitadelMaxAgeS: Int
        /// Whether the Cloud still accepts Zitadel access tokens next to its own session tokens.
        public var legacyZitadelTokens: Bool

        public init(enabled: Bool, zitadelMaxAgeS: Int, legacyZitadelTokens: Bool) {
            self.enabled = enabled
            self.zitadelMaxAgeS = zitadelMaxAgeS
            self.legacyZitadelTokens = legacyZitadelTokens
        }

        private enum CodingKeys: String, CodingKey {
            case enabled
            case zitadelMaxAgeS = "zitadel_max_age_s"
            case legacyZitadelTokens = "legacy_zitadel_tokens"
        }
    }

    public init(
        issuer: URL, projectId: String?, clients: Clients, scopes: [String], serverTokens: Bool, push: Push,
        sessions: SessionsConfig? = nil
    ) {
        self.issuer = issuer
        self.projectId = projectId
        self.clients = clients
        self.scopes = scopes
        self.serverTokens = serverTokens
        self.push = push
        self.sessions = sessions
    }

    private enum CodingKeys: String, CodingKey {
        case issuer
        case projectId = "project_id"
        case clients, scopes, push, sessions
        case serverTokens = "server_tokens"
    }
}

/// One agent of a server in the agenda (`PUT /v1/servers/{id}/agents`).
public struct CloudAgent: Codable, Sendable, Equatable {
    public var id: String
    public var slug: String
    public var displayName: String
    public var icon: String
    /// `conversation`, `one-shot` or `monologue`.
    public var callType: String
    /// The accent color; omitted from the agenda when `nil`.
    public var color: AgentColor?

    public init(id: String, slug: String, displayName: String, icon: String, callType: String, color: AgentColor? = nil) {
        self.id = id
        self.slug = slug
        self.displayName = displayName
        self.icon = icon
        self.callType = callType
        self.color = color
    }

    private enum CodingKeys: String, CodingKey {
        case id, slug, icon, color
        case displayName = "display_name"
        case callType = "call_type"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        slug = try container.decode(String.self, forKey: .slug)
        displayName = try container.decode(String.self, forKey: .displayName)
        icon = try container.decode(String.self, forKey: .icon)
        callType = try container.decode(String.self, forKey: .callType)
        color = container.decodeAgentColor(forKey: .color)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(slug, forKey: .slug)
        try container.encode(displayName, forKey: .displayName)
        try container.encode(icon, forKey: .icon)
        try container.encode(callType, forKey: .callType)
        try container.encodeIfPresent(color, forKey: .color)
    }
}

/// One server of the account's agenda. `url` is as the Cloud stored it: it accepts any `http://`, so check it
/// with `ServerAddress.parse` before talking to it.
public struct CloudServer: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var url: String
    /// `self-hosted` or `cloud`.
    public var kind: String
    public var linked: Bool
    public var agents: [CloudAgent]

    public init(id: String, name: String, url: String, kind: String, linked: Bool, agents: [CloudAgent]) {
        self.id = id
        self.name = name
        self.url = url
        self.kind = kind
        self.linked = linked
        self.agents = agents
    }

    /// The Wristcall Cloud's own entry in the agenda (`kind == "cloud"`): the Cloud routes calls to a node, it is
    /// not a server to pair with.
    public var isCloudEntry: Bool { kind == "cloud" }
}

/// `POST /v1/server-tokens`: a token for one server (`audience`), signed by the Cloud. A secret: `description`
/// and the mirror hide it.
public struct ServerToken: Decodable, Sendable {
    public var token: String
    public var audience: String
    /// Seconds since 1970.
    public var expiresAt: Double

    public init(token: String, audience: String, expiresAt: Double) {
        self.token = token
        self.audience = audience
        self.expiresAt = expiresAt
    }

    private enum CodingKeys: String, CodingKey {
        case token, audience
        case expiresAt = "expires_at"
    }
}

extension ServerToken: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "ServerToken(audience: \(audience), token: <redacted>, expiresAt: \(expiresAt))" }
    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: ["audience": audience, "token": "<redacted>", "expiresAt": expiresAt])
    }
}
