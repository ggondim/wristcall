import Foundation
import Security

// Models of the Cloud 0.4.0 account and session routes (`CloudClient+Sessions.swift`). Dates travel as Unix
// seconds. Secrets (account key, tokens, enroll code) print as `<redacted>`, like `LinkCode`.

/// The account key: 32 random bytes in canonical base64url without padding (43 characters). It proves
/// ownership of a silent account and is the only way back into it from a new device, so it lives in the
/// (iCloud) Keychain and nowhere else.
public struct AccountKey: Sendable, Hashable {
    /// Exactly 43 characters of `A`-`Z`, `a`-`z`, `0`-`9`, `-`, `_`.
    public let value: String

    /// `nil` unless `value` is the canonical base64url of 32 bytes (43 characters, no padding, no stray bits).
    public init?(_ value: String) {
        guard Self.isCanonical(value) else { return nil }
        self.value = value
    }

    /// A new key from the system's secure random generator.
    public static func generate() -> AccountKey {
        var bytes = [UInt8](repeating: 0, count: byteCount)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        // A generator that fails must never hand out a key made of zeros.
        guard status == errSecSuccess else { fatalError("SecRandomCopyBytes failed (\(status))") }
        return AccountKey(encoded: Self.encode(Data(bytes)))
    }

    private static let byteCount = 32
    private static let length = 43

    private init(encoded: String) { self.value = encoded }

    private static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func isCanonical(_ value: String) -> Bool {
        guard value.utf8.count == length else { return false }
        let alphabet = value.utf8.allSatisfy { byte in
            (byte >= 0x41 && byte <= 0x5A) || (byte >= 0x61 && byte <= 0x7A)
                || (byte >= 0x30 && byte <= 0x39) || byte == 0x2D || byte == 0x5F
        }
        guard alphabet else { return false }
        let standard = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/") + "="
        guard let data = Data(base64Encoded: standard), data.count == byteCount else { return false }
        // Re-encoding rules out a last character with non-zero padding bits (two keys for the same bytes).
        return encode(data) == value
    }
}

extension AccountKey: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "AccountKey(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["value": "<redacted>"]) }
}

/// Where a device runs; Cloud limits what each platform may do.
public enum DevicePlatform: String, Codable, Sendable, Equatable {
    case ios, ipados, macos, watchos, web
}

/// The device in a request body (`name`: 1 to 64 characters, no control or format characters).
public struct CloudDeviceInfo: Codable, Sendable, Equatable {
    public var name: String
    public var platform: DevicePlatform

    public init(name: String, platform: DevicePlatform) {
        self.name = name
        self.platform = platform
    }
}

/// A device session: a one hour access token and a rotating, single use refresh token.
public struct SessionTokens: Sendable, Equatable, Decodable {
    public var account: String
    public var device: String
    public var accessToken: String
    public var refreshToken: String
    public var expiresAt: Date
    /// Whether the account has an account key. Only `POST /v1/devices` answers it; `nil` elsewhere.
    public var keySet: Bool?

    public init(
        account: String, device: String, accessToken: String, refreshToken: String, expiresAt: Date, keySet: Bool? = nil
    ) {
        self.account = account
        self.device = device
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.expiresAt = expiresAt
        self.keySet = keySet
    }

    private enum CodingKeys: String, CodingKey {
        case account, device
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case expiresAt = "expires_at"
        case keySet = "key_set"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        account = try container.decode(String.self, forKey: .account)
        device = try container.decode(String.self, forKey: .device)
        accessToken = try container.decode(String.self, forKey: .accessToken)
        refreshToken = try container.decode(String.self, forKey: .refreshToken)
        expiresAt = try container.decodeUnixDate(forKey: .expiresAt)
        keySet = try container.decodeIfPresent(Bool.self, forKey: .keySet)
    }
}

extension SessionTokens: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String {
        "SessionTokens(account: \(account), device: \(device), accessToken: <redacted>, refreshToken: <redacted>, "
            + "expiresAt: \(expiresAt), keySet: \(keySet.map(String.init(describing:)) ?? "nil"))"
    }
    public var debugDescription: String { description }
    public var customMirror: Mirror {
        Mirror(self, children: [
            "account": account, "device": device, "accessToken": "<redacted>", "refreshToken": "<redacted>",
            "expiresAt": expiresAt, "keySet": keySet as Any,
        ])
    }
}

/// A beta plan.
public enum PlanKind: String, Codable, Sendable, Equatable {
    case full, custom
}

/// A plan of the account (`source`: why it has one, e.g. `beta` or `invite`); `expiresAt` is `nil` for no end.
public struct Plan: Codable, Sendable, Equatable {
    public var plan: PlanKind
    public var source: String
    public var expiresAt: Date?

    public init(plan: PlanKind, source: String, expiresAt: Date? = nil) {
        self.plan = plan
        self.source = source
        self.expiresAt = expiresAt
    }

    private enum CodingKeys: String, CodingKey {
        case plan, source
        case expiresAt = "expires_at"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        plan = try container.decode(PlanKind.self, forKey: .plan)
        source = try container.decode(String.self, forKey: .source)
        expiresAt = try container.decodeUnixDateIfPresent(forKey: .expiresAt)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(plan, forKey: .plan)
        try container.encode(source, forKey: .source)
        try container.encodeUnixDateIfPresent(expiresAt, forKey: .expiresAt)
    }
}

/// `GET /v1/account`.
public struct CloudAccountInfo: Codable, Sendable, Equatable {
    public var account: String
    public var createdAt: Date
    /// How many servers the account's agenda holds.
    public var servers: Int
    public var plan: Plan?
    /// The kinds of sign-in linked to the account (`["zitadel"]`).
    public var identities: [String]
    /// Whether the account has an account key.
    public var keySet: Bool

    public init(
        account: String, createdAt: Date, servers: Int, plan: Plan? = nil, identities: [String] = [], keySet: Bool
    ) {
        self.account = account
        self.createdAt = createdAt
        self.servers = servers
        self.plan = plan
        self.identities = identities
        self.keySet = keySet
    }

    /// No server, no plan and no sign-in: nothing the user would lose by dropping the account.
    public var isEmpty: Bool { servers == 0 && plan == nil && identities.isEmpty }

    private enum CodingKeys: String, CodingKey {
        case account, servers, plan, identities
        case createdAt = "created_at"
        case keySet = "key_set"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        account = try container.decode(String.self, forKey: .account)
        createdAt = try container.decodeUnixDate(forKey: .createdAt)
        servers = try container.decode(Int.self, forKey: .servers)
        plan = try container.decodeIfPresent(Plan.self, forKey: .plan)
        identities = try container.decode([String].self, forKey: .identities)
        keySet = try container.decode(Bool.self, forKey: .keySet)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(account, forKey: .account)
        try container.encode(createdAt.timeIntervalSince1970, forKey: .createdAt)
        try container.encode(servers, forKey: .servers)
        try container.encode(plan, forKey: .plan)
        try container.encode(identities, forKey: .identities)
        try container.encode(keySet, forKey: .keySet)
    }
}

/// One entry of `GET /v1/devices`. `platform` stays a string: a newer Cloud may add platforms.
public struct AccountDevice: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var platform: String
    public var createdAt: Date
    public var lastSeenAt: Date?
    /// Whether it is the device that asked.
    public var current: Bool

    public init(id: String, name: String, platform: String, createdAt: Date, lastSeenAt: Date? = nil, current: Bool) {
        self.id = id
        self.name = name
        self.platform = platform
        self.createdAt = createdAt
        self.lastSeenAt = lastSeenAt
        self.current = current
    }

    private enum CodingKeys: String, CodingKey {
        case id, name, platform, current
        case createdAt = "created_at"
        case lastSeenAt = "last_seen_at"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        platform = try container.decode(String.self, forKey: .platform)
        createdAt = try container.decodeUnixDate(forKey: .createdAt)
        lastSeenAt = try container.decodeUnixDateIfPresent(forKey: .lastSeenAt)
        current = try container.decode(Bool.self, forKey: .current)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(name, forKey: .name)
        try container.encode(platform, forKey: .platform)
        try container.encode(createdAt.timeIntervalSince1970, forKey: .createdAt)
        try container.encodeUnixDateIfPresent(lastSeenAt, forKey: .lastSeenAt)
        try container.encode(current, forKey: .current)
    }
}

/// A single use code that lets a watch sign in (`POST /v1/devices/enroll`); valid for 10 minutes.
public struct EnrollCode: Sendable, Equatable, Decodable {
    public var code: String
    public var expiresAt: Date

    public init(code: String, expiresAt: Date) {
        self.code = code
        self.expiresAt = expiresAt
    }

    private enum CodingKeys: String, CodingKey {
        case code
        case expiresAt = "expires_at"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        code = try container.decode(String.self, forKey: .code)
        expiresAt = try container.decodeUnixDate(forKey: .expiresAt)
    }
}

extension EnrollCode: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "EnrollCode(code: <redacted>, expiresAt: \(expiresAt))" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["code": "<redacted>", "expiresAt": expiresAt]) }
}

/// What a browser asking for a web session looks like (`POST /v1/web-sessions/{id}/inspect`), shown to the
/// user before they approve it.
public struct WebSessionInfo: Codable, Sendable, Equatable {
    public var name: String
    public var userAgent: String?
    /// ISO 3166 country code, if Cloud knows it.
    public var country: String?
    public var createdAt: Date
    public var expiresAt: Date

    public init(name: String, userAgent: String? = nil, country: String? = nil, createdAt: Date, expiresAt: Date) {
        self.name = name
        self.userAgent = userAgent
        self.country = country
        self.createdAt = createdAt
        self.expiresAt = expiresAt
    }

    private enum CodingKeys: String, CodingKey {
        case name, country
        case userAgent = "user_agent"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        userAgent = try container.decodeIfPresent(String.self, forKey: .userAgent)
        country = try container.decodeIfPresent(String.self, forKey: .country)
        createdAt = try container.decodeUnixDate(forKey: .createdAt)
        expiresAt = try container.decodeUnixDate(forKey: .expiresAt)
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(name, forKey: .name)
        try container.encodeIfPresent(userAgent, forKey: .userAgent)
        try container.encodeIfPresent(country, forKey: .country)
        try container.encode(createdAt.timeIntervalSince1970, forKey: .createdAt)
        try container.encode(expiresAt.timeIntervalSince1970, forKey: .expiresAt)
    }
}

// MARK: - Unix seconds

extension KeyedDecodingContainer {
    /// A number of Unix seconds (`JSONDecoder` would read a `Date` as seconds since 2001).
    func decodeUnixDate(forKey key: Key) throws -> Date {
        Date(timeIntervalSince1970: try decode(Double.self, forKey: key))
    }

    /// `nil` for a missing key or `null`.
    func decodeUnixDateIfPresent(forKey key: Key) throws -> Date? {
        try decodeIfPresent(Double.self, forKey: key).map { Date(timeIntervalSince1970: $0) }
    }
}

extension KeyedEncodingContainer {
    /// Omits a `nil` date.
    mutating func encodeUnixDateIfPresent(_ date: Date?, forKey key: Key) throws {
        try encodeIfPresent(date?.timeIntervalSince1970, forKey: key)
    }
}
