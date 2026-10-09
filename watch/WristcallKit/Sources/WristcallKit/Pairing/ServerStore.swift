import Foundation
import Security
import Synchronization

/// Persistence of the servers this watch is paired with, in the order the agent grid shows them.
public protocol ServerStore: Sendable {
    /// Empty when the watch is not paired with any server.
    func load() throws -> [Credentials]
    /// Replaces the whole list. An empty list removes what was stored.
    func save(_ servers: [Credentials]) throws
    /// Removes the list and anything left from 0.1.0. Succeeds when nothing is stored.
    func deleteAll() throws
}

/// For tests and SwiftUI previews.
public final class InMemoryServerStore: ServerStore {
    private let stored: Mutex<[Credentials]>

    public init(_ servers: [Credentials] = []) {
        stored = Mutex(servers)
    }

    public func load() throws -> [Credentials] {
        stored.withLock { $0 }
    }

    public func save(_ servers: [Credentials]) throws {
        stored.withLock { $0 = servers }
    }

    public func deleteAll() throws {
        stored.withLock { $0 = [] }
    }
}

/// Keeps the server list as one JSON array in one generic password item, readable after the first
/// unlock (so a call works with the wrist down) and never synced or restored to another device.
///
/// 0.1.0 kept a single `Credentials` item (`KeychainCredentialStore`, account `default`); the first
/// `load()` moves it into the list.
public struct KeychainServerStore: ServerStore {
    public static let defaultService = KeychainCredentialStore.defaultService

    public let service: String
    public let account: String

    /// Tests pass their own `service` so they never touch the app's item.
    public init(service: String = Self.defaultService, account: String = "servers") {
        self.service = service
        self.account = account
    }

    public func load() throws -> [Credentials] {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data,
                  let servers = try? JSONDecoder().decode([Credentials].self, from: data)
            else { throw CredentialStoreError.corruptedData }
            return servers
        case errSecItemNotFound:
            return try migrateLegacy()
        default:
            throw CredentialStoreError.keychain(status)
        }
    }

    public func save(_ servers: [Credentials]) throws {
        guard !servers.isEmpty else { return try deleteAll() }
        let data = try JSONEncoder().encode(servers)
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

    /// Also drops the 0.1.0 item: otherwise the next `load()` would bring the removed server back.
    public func deleteAll() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CredentialStoreError.keychain(status)
        }
        try legacyStore.delete()
    }

    /// The 0.1.0 item becomes the first server. It is deleted only after the list is saved, so a
    /// failure in between loses nothing.
    private func migrateLegacy() throws -> [Credentials] {
        guard let legacy = try legacyStore.load() else { return [] }
        try save([legacy])
        // The list already holds it; a leftover item is cleaned up by the next `deleteAll()`.
        try? legacyStore.delete()
        return [legacy]
    }

    private var legacyStore: KeychainCredentialStore {
        KeychainCredentialStore(service: service, account: "default")
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            // See `KeychainCredentialStore`: the iOS-style keychain, which needs a signed app.
            kSecUseDataProtectionKeychain as String: true,
        ]
    }
}
