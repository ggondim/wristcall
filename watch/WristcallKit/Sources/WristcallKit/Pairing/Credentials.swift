import Foundation
import Synchronization

/// What the watch keeps after pairing: where the server is and how to authenticate.
public struct Credentials: Codable, Sendable, Equatable {
    /// Local id of this server on this watch (not the device id): it names the server in `AgentRef`s
    /// and widget configuration, and survives the server being re-paired under another URL.
    public var id: String
    public var serverURL: URL
    public var deviceId: String
    /// Bearer token. Lives only in the Keychain (`KeychainServerStore`); never log it.
    public var token: String

    public init(serverURL: URL, deviceId: String, token: String, id: String = UUID().uuidString) {
        self.id = id
        self.serverURL = serverURL
        self.deviceId = deviceId
        self.token = token
    }

    public init(serverURL: URL, device: PairedDevice, id: String = UUID().uuidString) {
        self.init(serverURL: serverURL, deviceId: device.deviceId, token: device.token, id: id)
    }

    private enum CodingKeys: String, CodingKey {
        case id, serverURL, deviceId, token
    }

    /// An item written by 0.1.0 has no `id`: it gets one, and keeps it once the list is saved.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        serverURL = try container.decode(URL.self, forKey: .serverURL)
        deviceId = try container.decode(String.self, forKey: .deviceId)
        token = try container.decode(String.self, forKey: .token)
    }
}

extension Credentials: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String {
        "Credentials(serverURL: \(serverURL), deviceId: \(deviceId), token: <redacted>)"
    }

    public var debugDescription: String { description }
}

/// Persistence of the single set of credentials of this watch.
public protocol CredentialStore: Sendable {
    /// `nil` when the watch is not paired.
    func load() throws -> Credentials?
    /// Replaces whatever was stored.
    func save(_ credentials: Credentials) throws
    /// Succeeds when nothing is stored.
    func delete() throws
}

/// For tests and SwiftUI previews.
public final class InMemoryCredentialStore: CredentialStore {
    private let stored: Mutex<Credentials?>

    public init(_ credentials: Credentials? = nil) {
        stored = Mutex(credentials)
    }

    public func load() throws -> Credentials? {
        stored.withLock { $0 }
    }

    public func save(_ credentials: Credentials) throws {
        stored.withLock { $0 = credentials }
    }

    public func delete() throws {
        stored.withLock { $0 = nil }
    }
}
