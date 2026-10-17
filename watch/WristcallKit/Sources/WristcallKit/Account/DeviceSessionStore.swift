import Foundation
import Synchronization

/// The device session the Cloud gave this device (`POST /v1/accounts`, `/v1/devices`, `/v1/session/refresh`).
/// The refresh token is single use: every refresh replaces the pair, so the store always holds the latest one.
public struct StoredDeviceSession: Codable, Equatable, Sendable {
    public var account: String
    public var device: String
    public var refreshToken: String
    public var accessToken: String
    public var expiresAt: Date

    public init(account: String, device: String, refreshToken: String, accessToken: String, expiresAt: Date) {
        self.account = account
        self.device = device
        self.refreshToken = refreshToken
        self.accessToken = accessToken
        self.expiresAt = expiresAt
    }

    public init(_ tokens: SessionTokens) {
        self.init(
            account: tokens.account, device: tokens.device, refreshToken: tokens.refreshToken,
            accessToken: tokens.accessToken, expiresAt: tokens.expiresAt
        )
    }

    /// Whether the access token still has at least `margin` seconds to live at `now`.
    func isFresh(at now: Date, margin: TimeInterval = 60) -> Bool {
        expiresAt.timeIntervalSince(now) >= margin
    }

    private enum CodingKeys: String, CodingKey {
        case account, device, refreshToken, accessToken, expiresAt
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        account = try container.decode(String.self, forKey: .account)
        device = try container.decode(String.self, forKey: .device)
        refreshToken = try container.decode(String.self, forKey: .refreshToken)
        accessToken = try container.decode(String.self, forKey: .accessToken)
        expiresAt = Date(timeIntervalSince1970: try container.decode(Double.self, forKey: .expiresAt))
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(account, forKey: .account)
        try container.encode(device, forKey: .device)
        try container.encode(refreshToken, forKey: .refreshToken)
        try container.encode(accessToken, forKey: .accessToken)
        try container.encode(expiresAt.timeIntervalSince1970, forKey: .expiresAt)
    }
}

extension StoredDeviceSession: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String {
        "StoredDeviceSession(account: \(account), device: \(device), refreshToken: <redacted>, "
            + "accessToken: <redacted>, expiresAt: \(expiresAt))"
    }
    public var debugDescription: String { description }
    public var customMirror: Mirror {
        Mirror(self, children: [
            "account": account, "device": device, "refreshToken": "<redacted>", "accessToken": "<redacted>",
            "expiresAt": expiresAt,
        ])
    }
}

/// Where `DeviceSession` keeps its session.
public protocol DeviceSessionStore: Sendable {
    func load() throws -> StoredDeviceSession?
    func save(_ session: StoredDeviceSession) throws
    func clear() throws
}

/// Keeps the session as one JSON item in the Keychain, readable after the first unlock and never synced or
/// restored to another device. Errors are `CredentialStoreError`; `.keychain(errSecInteractionNotAllowed)` means
/// "not readable right now", not "signed out".
public struct KeychainDeviceSessionStore: DeviceSessionStore {
    public static let defaultService = "io.github.ggondim.wristcall.session"

    public let service: String

    /// Tests pass their own `service` so they never touch the app's item.
    public init(service: String = Self.defaultService) {
        self.service = service
    }

    public func load() throws -> StoredDeviceSession? {
        guard let data = try item.read() else { return nil }
        guard let session = try? JSONDecoder().decode(StoredDeviceSession.self, from: data) else {
            throw CredentialStoreError.corruptedData
        }
        return session
    }

    public func save(_ session: StoredDeviceSession) throws {
        try item.write(try JSONEncoder().encode(session))
    }

    public func clear() throws {
        try item.delete()
    }

    private var item: KeychainItem { KeychainItem(service: service, account: "device") }
}
