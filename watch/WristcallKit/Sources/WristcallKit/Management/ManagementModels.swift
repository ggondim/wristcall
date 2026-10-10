import Foundation

/// An agent as its owner sees it (`GET /v1/agents`, `/v1/agents/{ref}` with a personal token).
/// Secrets in endpoints come back redacted (`***`); sending `***` back keeps the stored value.
public struct AgentDetail: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var slug: String
    public var displayName: String
    public var icon: String
    public var callType: String
    public var turnEnd: String
    public var position: Int
    public var language: String
    public var stt: JSONValue?
    public var action: JSONValue?
    public var tts: JSONValue?
    public var systemPrompt: String
    public var fallbackMessage: String
    public var vad: [String: JSONValue]
    public var timeouts: [String: JSONValue]
    /// `.int` (days), `.string("forever")` or `.null` (the server's default applies).
    public var retentionDays: JSONValue
    /// The retention in force, when the server sends it (`.null`: kept until deleted).
    public var effectiveRetentionDays: JSONValue?
    public var createdAt: Double
    public var updatedAt: Double

    /// `one-shot` and `monologue` agents record and never answer.
    public var isOneWay: Bool { callType == "one-shot" || callType == "monologue" }

    public init(
        id: String, slug: String, displayName: String, icon: String = "waveform", callType: String = "conversation",
        turnEnd: String = "auto", position: Int = 0, language: String = "en", stt: JSONValue? = nil,
        action: JSONValue? = nil, tts: JSONValue? = nil, systemPrompt: String = "", fallbackMessage: String = "",
        vad: [String: JSONValue] = [:], timeouts: [String: JSONValue] = [:], retentionDays: JSONValue = .null,
        effectiveRetentionDays: JSONValue? = nil, createdAt: Double = 0, updatedAt: Double = 0
    ) {
        self.id = id
        self.slug = slug
        self.displayName = displayName
        self.icon = icon
        self.callType = callType
        self.turnEnd = turnEnd
        self.position = position
        self.language = language
        self.stt = stt
        self.action = action
        self.tts = tts
        self.systemPrompt = systemPrompt
        self.fallbackMessage = fallbackMessage
        self.vad = vad
        self.timeouts = timeouts
        self.retentionDays = retentionDays
        self.effectiveRetentionDays = effectiveRetentionDays
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    private enum CodingKeys: String, CodingKey {
        case id, slug, icon, position, language, stt, action, tts, vad, timeouts
        case displayName = "display_name"
        case callType = "call_type"
        case turnEnd = "turn_end"
        case systemPrompt = "system_prompt"
        case fallbackMessage = "fallback_message"
        case retentionDays = "retention_days"
        case effectiveRetentionDays = "effective_retention_days"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        slug = try container.decode(String.self, forKey: .slug)
        displayName = try container.decode(String.self, forKey: .displayName)
        icon = try container.decode(String.self, forKey: .icon)
        callType = try container.decode(String.self, forKey: .callType)
        turnEnd = try container.decode(String.self, forKey: .turnEnd)
        position = try container.decode(Int.self, forKey: .position)
        language = try container.decode(String.self, forKey: .language)
        stt = try container.decodeIfPresent(JSONValue.self, forKey: .stt)
        action = try container.decodeIfPresent(JSONValue.self, forKey: .action)
        tts = try container.decodeIfPresent(JSONValue.self, forKey: .tts)
        systemPrompt = try container.decode(String.self, forKey: .systemPrompt)
        fallbackMessage = try container.decode(String.self, forKey: .fallbackMessage)
        vad = try container.decodeIfPresent([String: JSONValue].self, forKey: .vad) ?? [:]
        timeouts = try container.decodeIfPresent([String: JSONValue].self, forKey: .timeouts) ?? [:]
        retentionDays = try container.decodeIfPresent(JSONValue.self, forKey: .retentionDays) ?? .null
        // Absent (the server has no history settings) differs from `null` (kept until deleted).
        effectiveRetentionDays = container.contains(.effectiveRetentionDays)
            ? try container.decode(JSONValue.self, forKey: .effectiveRetentionDays) : nil
        createdAt = try container.decode(Double.self, forKey: .createdAt)
        updatedAt = try container.decode(Double.self, forKey: .updatedAt)
    }
}

/// Body of `POST /v1/agents` and `PATCH /v1/agents/{ref}`. A missing key leaves the field alone; an
/// explicit `.null` sends `null` (e.g. `retention_days` back to the server's default).
public typealias AgentFields = [String: JSONValue]

public struct Provider: Decodable, Sendable, Hashable {
    public var name: String
    /// `stt`, `responder`, `tts` or `webhook`.
    public var kind: String

    public init(name: String, kind: String) {
        self.name = name
        self.kind = kind
    }
}

public struct ProviderList: Decodable, Sendable, Equatable {
    public var providers: [Provider]
    public var customEndpoints: Bool

    public init(providers: [Provider], customEndpoints: Bool) {
        self.providers = providers
        self.customEndpoints = customEndpoints
    }

    private enum CodingKeys: String, CodingKey {
        case providers
        case customEndpoints = "custom_endpoints"
    }
}

public struct DeviceRecord: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var createdAt: Double

    public init(id: String, name: String, createdAt: Double) {
        self.id = id
        self.name = name
        self.createdAt = createdAt
    }

    private enum CodingKeys: String, CodingKey {
        case id, name
        case createdAt = "created_at"
    }
}

/// `POST /v1/pairing-codes`. The code is a secret for 10 minutes: `description` hides it.
public struct PairingCodeGrant: Decodable, Sendable, Equatable {
    public var code: String
    public var expiresAt: Double
    public var serverUrl: String
    public var viaDirectory: Bool
    public var warning: String?

    public init(code: String, expiresAt: Double, serverUrl: String, viaDirectory: Bool, warning: String? = nil) {
        self.code = code
        self.expiresAt = expiresAt
        self.serverUrl = serverUrl
        self.viaDirectory = viaDirectory
        self.warning = warning
    }

    private enum CodingKeys: String, CodingKey {
        case code, warning
        case expiresAt = "expires_at"
        case serverUrl = "server_url"
        case viaDirectory = "via_directory"
    }
}

extension PairingCodeGrant: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String {
        "PairingCodeGrant(code: <redacted>, expiresAt: \(expiresAt), serverUrl: \(serverUrl), viaDirectory: \(viaDirectory))"
    }

    public var debugDescription: String { description }
}

/// A watch waiting for approval (`GET /v1/pairing-requests`).
public struct ApprovalRequest: Decodable, Sendable, Equatable, Identifiable {
    public var requestId: String
    public var deviceName: String
    public var expiresAt: Double
    public var id: String { requestId }

    public init(requestId: String, deviceName: String, expiresAt: Double) {
        self.requestId = requestId
        self.deviceName = deviceName
        self.expiresAt = expiresAt
    }

    private enum CodingKeys: String, CodingKey {
        case requestId = "request_id"
        case deviceName = "device_name"
        case expiresAt = "expires_at"
    }
}

/// `POST /v1/account/link`. `apiToken` (a personal token) comes only from the link by code: `description`
/// hides it.
public struct AccountLink: Decodable, Sendable, Equatable {
    public var linked: Bool
    public var issuer: String
    public var user: LinkedUser?
    public var apiToken: String?

    public struct LinkedUser: Decodable, Sendable, Equatable {
        public var id: String
        public var handle: String

        public init(id: String, handle: String) {
            self.id = id
            self.handle = handle
        }
    }

    public init(linked: Bool, issuer: String, user: LinkedUser? = nil, apiToken: String? = nil) {
        self.linked = linked
        self.issuer = issuer
        self.user = user
        self.apiToken = apiToken
    }

    private enum CodingKeys: String, CodingKey {
        case linked, issuer, user
        case apiToken = "api_token"
    }
}

extension AccountLink: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String {
        "AccountLink(linked: \(linked), issuer: \(issuer), user: \(user.map(\.handle) ?? "nil"), "
            + "apiToken: \(apiToken == nil ? "nil" : "<redacted>"))"
    }

    public var debugDescription: String { description }
}
