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

    public init(issuer: URL, projectId: String?, clients: Clients, scopes: [String], serverTokens: Bool, push: Push) {
        self.issuer = issuer
        self.projectId = projectId
        self.clients = clients
        self.scopes = scopes
        self.serverTokens = serverTokens
        self.push = push
    }

    private enum CodingKeys: String, CodingKey {
        case issuer
        case projectId = "project_id"
        case clients, scopes, push
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

    public init(id: String, slug: String, displayName: String, icon: String, callType: String) {
        self.id = id
        self.slug = slug
        self.displayName = displayName
        self.icon = icon
        self.callType = callType
    }

    private enum CodingKeys: String, CodingKey {
        case id, slug, icon
        case displayName = "display_name"
        case callType = "call_type"
    }
}

/// One server of the account's agenda. `url` is as the Cloud stored it: it accepts any `http://`, so check it
/// with `ServerAddress.parse` before talking to it.
public struct CloudServer: Decodable, Sendable, Equatable, Identifiable {
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
