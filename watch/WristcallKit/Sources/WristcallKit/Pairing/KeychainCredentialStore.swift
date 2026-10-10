import Foundation
import Security

/// Errors of `KeychainCredentialStore`.
public enum CredentialStoreError: Error, Sendable, Equatable {
    /// A Keychain call failed with this `OSStatus`. `errSecInteractionNotAllowed` (-25308) means the
    /// item is not readable right now (device locked before its first unlock): retry later, do not delete.
    case keychain(OSStatus)
    /// The stored item is not valid `Credentials` JSON.
    case corruptedData

    public var isInteractionNotAllowed: Bool { self == .keychain(errSecInteractionNotAllowed) }
}

/// Keeps `Credentials` as one JSON generic password item in the Keychain, readable after the
/// first unlock (so a call works with the wrist down) and never synced or restored to another device.
public struct KeychainCredentialStore: CredentialStore {
    public static let defaultService = "io.github.ggondim.wristcall.credentials"

    public let service: String
    public let account: String

    /// Tests pass their own `service` so they never touch the app's item.
    public init(service: String = Self.defaultService, account: String = "default") {
        self.service = service
        self.account = account
    }

    public func load() throws -> Credentials? {
        guard let data = try item.read() else { return nil }
        guard let credentials = try? JSONDecoder().decode(Credentials.self, from: data) else {
            throw CredentialStoreError.corruptedData
        }
        return credentials
    }

    public func save(_ credentials: Credentials) throws {
        try item.write(try JSONEncoder().encode(credentials))
    }

    public func delete() throws {
        try item.delete()
    }

    private var item: KeychainItem { KeychainItem(service: service, account: account) }
}
