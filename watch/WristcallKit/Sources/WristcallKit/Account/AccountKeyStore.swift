import CryptoKit
import Foundation
import Security

/// An account key kept in the (iCloud) Keychain, with the account it opens and when it was first stored.
public struct StoredAccountKey: Equatable, Sendable {
    public let account: String
    public let key: AccountKey
    public let createdAt: Date

    public init(account: String, key: AccountKey, createdAt: Date) {
        self.account = account
        self.key = key
        self.createdAt = createdAt
    }
}

/// Where the account keys live. Two kinds of item, never mixed:
/// - the *synced* keys (one per account, the ones `keys()` lists): they travel through iCloud Keychain to the
///   user's other devices, and nothing but the user ever deletes them (a `401` only marks a key dead, see
///   `DeadKeys`);
/// - the *pending* key: this device only, written before a key is sent to the Cloud (sign-up, or the migration's
///   `PUT`) so a lost reply never loses the key. It is promoted to a synced key once the Cloud confirms it.
public protocol AccountKeyStore: Sendable {
    /// The synced keys, oldest first.
    func keys() throws -> [StoredAccountKey]
    /// Adds the key of `account`, or replaces the one it already has. Saving the key the account already
    /// holds changes nothing (it keeps its `createdAt`).
    func save(_ key: AccountKey, account: String) throws
    func pending() throws -> AccountKey?
    func savePending(_ key: AccountKey) throws
    func clearPending() throws
    /// Deletes the synced key of `account` (and its copies on the user's other devices).
    func delete(account: String) throws
    /// Deletes every synced key and the pending key: nothing is left.
    func deleteAll() throws
}

/// The keys as generic password items in the data protection Keychain.
///
/// Synced items: service `service`, `kSecAttrAccount` the account id, `kSecAttrSynchronizable` true, readable
/// after the first unlock, in `accessGroup` (when given); the data is `{"key": "<43>", "created_at": <Unix seconds>}`.
/// The pending item: service `pendingService`, account `pending`, this device only.
///
/// Every query, update and delete of a synced item names `kSecAttrSynchronizable` (`Any` when it looks items up
/// or removes them), or the Keychain would silently skip them. There is no fallback: a Keychain that refuses
/// (`errSecMissingEntitlement`, -34018, when the app lacks the Keychain access group) throws
/// `CredentialStoreError.keychain`.
public struct KeychainAccountKeyStore: AccountKeyStore {
    public static let service = "io.github.ggondim.wristcall.account-key"
    public static let pendingService = "io.github.ggondim.wristcall.account-key.pending"

    public let accessGroup: String?
    public let service: String
    public let pendingService: String

    /// `accessGroup`: `<AppIdentifierPrefix>io.github.ggondim.wristcall.shared`, or `nil` for the app's default
    /// group (tests). Tests also pass their own services so they never touch the app's items.
    public init(
        accessGroup: String?, service: String = KeychainAccountKeyStore.service,
        pendingService: String = KeychainAccountKeyStore.pendingService
    ) {
        self.accessGroup = accessGroup
        self.service = service
        self.pendingService = pendingService
    }

    public func keys() throws -> [StoredAccountKey] {
        var query = syncedQuery(account: nil)
        query[kSecMatchLimit as String] = kSecMatchLimitAll
        query[kSecReturnAttributes as String] = true
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return [] }
        guard status == errSecSuccess else { throw CredentialStoreError.keychain(status) }
        guard let items = result as? [[String: Any]] else { throw CredentialStoreError.corruptedData }
        // An item that is not ours (or damaged) is skipped, never deleted: it may be a newer app's format.
        return items.compactMap { item in
            guard let account = item[kSecAttrAccount as String] as? String,
                  let data = item[kSecValueData as String] as? Data
            else { return nil }
            return Self.decode(data, account: account)
        }
        .sorted { ($0.createdAt, $0.account) < ($1.createdAt, $1.account) }
    }

    public func save(_ key: AccountKey, account: String) throws {
        if try keys().contains(where: { $0.account == account && $0.key == key }) { return }
        let data = try JSONEncoder().encode(Payload(key: key.value, createdAt: Date().timeIntervalSince1970))
        let query = syncedQuery(account: account)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var attributes = syncedQuery(account: account, synchronizable: true)
            attributes[kSecValueData as String] = data
            attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            status = SecItemAdd(attributes as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw CredentialStoreError.keychain(status) }
    }

    public func pending() throws -> AccountKey? {
        // A pending item that is not a key cannot be sent anywhere: the next `savePending` replaces it.
        guard let data = try pendingItem.read(), let text = String(data: data, encoding: .utf8) else { return nil }
        return AccountKey(text)
    }

    public func savePending(_ key: AccountKey) throws {
        try pendingItem.write(Data(key.value.utf8))
    }

    public func clearPending() throws {
        try pendingItem.delete()
    }

    public func delete(account: String) throws {
        try remove(syncedQuery(account: account))
    }

    public func deleteAll() throws {
        try remove(syncedQuery(account: nil))
        try clearPending()
    }

    private func remove(_ query: [String: Any]) throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw CredentialStoreError.keychain(status) }
    }

    /// `synchronizable`: `true` when adding; omitted, it matches items of either kind.
    private func syncedQuery(account: String?, synchronizable: Bool? = nil) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrSynchronizable as String: synchronizable ?? kSecAttrSynchronizableAny,
            kSecUseDataProtectionKeychain as String: true,
        ]
        if let account { query[kSecAttrAccount as String] = account }
        if let accessGroup { query[kSecAttrAccessGroup as String] = accessGroup }
        return query
    }

    private var pendingItem: KeychainItem { KeychainItem(service: pendingService, account: "pending") }

    private struct Payload: Codable {
        let key: String
        let createdAt: Double

        enum CodingKeys: String, CodingKey {
            case key
            case createdAt = "created_at"
        }
    }

    private static func decode(_ data: Data, account: String) -> StoredAccountKey? {
        guard let payload = try? JSONDecoder().decode(Payload.self, from: data), let key = AccountKey(payload.key)
        else { return nil }
        return StoredAccountKey(account: account, key: key, createdAt: Date(timeIntervalSince1970: payload.createdAt))
    }
}

/// Keys the Cloud refused (`401`), remembered on this device only. A refused key is skipped from then on but
/// its Keychain item stays: a `401` can be a wrong guess of this device, and the item belongs to every device
/// the user owns. Only `"<account>:<SHA-256 of the key, hex>"` is stored, never the key.
public struct DeadKeys {
    static let defaultsKey = "account.deadKeys"

    public static func isDead(_ key: StoredAccountKey, defaults: UserDefaults = .standard) -> Bool {
        (defaults.stringArray(forKey: defaultsKey) ?? []).contains(entry(for: key))
    }

    public static func mark(_ key: StoredAccountKey, defaults: UserDefaults = .standard) {
        var entries = defaults.stringArray(forKey: defaultsKey) ?? []
        let entry = entry(for: key)
        guard !entries.contains(entry) else { return }
        entries.append(entry)
        defaults.set(entries, forKey: defaultsKey)
    }

    /// Forgets every refused key (the account was deleted: nothing of it is left to skip).
    public static func forgetAll(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: defaultsKey)
    }

    private static func entry(for key: StoredAccountKey) -> String {
        let digest = SHA256.hash(data: Data(key.key.value.utf8))
        return key.account + ":" + digest.map { String(format: "%02x", $0) }.joined()
    }
}
