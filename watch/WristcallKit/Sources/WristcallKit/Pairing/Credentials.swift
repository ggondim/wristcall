import Foundation
import Synchronization

/// What the watch keeps after pairing: where the server is and how to authenticate.
public struct Credentials: Codable, Sendable, Equatable {
    public var serverURL: URL
    public var deviceId: String
    /// Bearer token. Lives only in the Keychain (`KeychainCredentialStore`); never log it.
    public var token: String

    public init(serverURL: URL, deviceId: String, token: String) {
        self.serverURL = serverURL
        self.deviceId = deviceId
        self.token = token
    }

    public init(serverURL: URL, device: PairedDevice) {
        self.init(serverURL: serverURL, deviceId: device.deviceId, token: device.token)
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
