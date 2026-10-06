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
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data,
                  let credentials = try? JSONDecoder().decode(Credentials.self, from: data)
            else { throw CredentialStoreError.corruptedData }
            return credentials
        case errSecItemNotFound:
            return nil
        default:
            throw CredentialStoreError.keychain(status)
        }
    }

    public func save(_ credentials: Credentials) throws {
        let data = try JSONEncoder().encode(credentials)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        var status = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(baseQuery.merging(attributes) { $1 } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw CredentialStoreError.keychain(status) }
    }

    public func delete() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.keychain(status)
        }
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            // On macOS this selects the iOS-style keychain (the one watchOS always uses) instead of the
            // file-based login keychain; it needs a signed app with an application identifier.
            kSecUseDataProtectionKeychain as String: true,
        ]
    }
}
