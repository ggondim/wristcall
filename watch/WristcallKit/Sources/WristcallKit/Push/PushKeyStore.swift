import Foundation

/// The push key of one server and the APNs token it was registered with. Both are secrets: `description` and
/// the mirror hide the key.
public struct StoredPushKey: Codable, Equatable, Sendable {
    public var pushKey: String
    /// Hex of the APNs device token: a new token means a new registration.
    public var deviceToken: String

    public init(pushKey: String, deviceToken: String) {
        self.pushKey = pushKey
        self.deviceToken = deviceToken
    }
}

/// Where the push keys live, one per server (the local server id, `Credentials.id`).
public protocol PushKeyStore: Sendable {
    func load(serverID: String) throws -> StoredPushKey?
    func save(_ key: StoredPushKey, serverID: String) throws
    func delete(serverID: String) throws
}

/// One generic password item per server in the service of the paired servers, account `push.<server id>`,
/// readable after the first unlock and never synced, like the servers' tokens. Errors are `CredentialStoreError`.
public struct KeychainPushKeyStore: PushKeyStore {
    public var service: String

    public init(service: String = KeychainCredentialStore.defaultService) {
        self.service = service
    }

    public func load(serverID: String) throws -> StoredPushKey? {
        guard let data = try item(serverID).read() else { return nil }
        guard let key = try? JSONDecoder().decode(StoredPushKey.self, from: data) else {
            throw CredentialStoreError.corruptedData
        }
        return key
    }

    public func save(_ key: StoredPushKey, serverID: String) throws {
        try item(serverID).write(try JSONEncoder().encode(key))
    }

    public func delete(serverID: String) throws {
        try item(serverID).delete()
    }

    private func item(_ serverID: String) -> KeychainItem { KeychainItem(service: service, account: "push.\(serverID)") }
}

extension StoredPushKey: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "StoredPushKey(pushKey: <redacted>, deviceToken: <redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["pushKey": "<redacted>", "deviceToken": "<redacted>"]) }
}
