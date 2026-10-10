import Foundation
import Synchronization

/// A server the iPhone app manages, with the personal token (`wc_pat_…`) it uses on `/v1/*`
/// management routes. Never log the token: `description` hides it.
public struct ManagedServer: Codable, Sendable, Equatable, Identifiable {
    /// Local id (a UUID); also the `tag` of the push registration made for this server.
    public var id: String
    /// 1 to 64 characters; defaults to the host.
    public var name: String
    public var url: URL
    /// Personal token (`wc_pat_…`).
    public var token: String
    /// The server's id in the Cloud agenda; `nil` until the account is linked.
    public var cloudServerID: String?
    /// `true` once the account link succeeded for this server.
    public var linked: Bool

    public init(
        id: String = UUID().uuidString, name: String, url: URL, token: String,
        cloudServerID: String? = nil, linked: Bool = false
    ) {
        self.id = id
        self.name = name
        self.url = url
        self.token = token
        self.cloudServerID = cloudServerID
        self.linked = linked
    }

    private enum CodingKeys: String, CodingKey {
        case id, name, url, token, cloudServerID, linked
    }

    /// `cloudServerID` and `linked` are optional in storage (items written before them decode).
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        url = try container.decode(URL.self, forKey: .url)
        token = try container.decode(String.self, forKey: .token)
        cloudServerID = try container.decodeIfPresent(String.self, forKey: .cloudServerID)
        linked = try container.decodeIfPresent(Bool.self, forKey: .linked) ?? false
    }
}

extension ManagedServer: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String {
        "ManagedServer(id: \(id), name: \(name), url: \(url), token: <redacted>)"
    }
    public var debugDescription: String { description }
}

/// Persistence for the iPhone app's server list.
public protocol ManagedServerStore: Sendable {
    func load() throws -> [ManagedServer]
    /// Replaces the whole list; an empty list removes the stored item.
    func save(_ servers: [ManagedServer]) throws
}

/// Keeps the list as one JSON array in one generic password item (see `KeychainItem`): readable
/// after the first unlock, never synced or restored to another device. Errors are
/// `CredentialStoreError`.
public struct KeychainManagedServerStore: ManagedServerStore {
    public static let defaultService = "io.github.ggondim.wristcall.phone"

    public let service: String
    public let account: String

    /// Tests pass their own `service` so they never touch the app's item.
    public init(service: String = Self.defaultService, account: String = "servers") {
        self.service = service
        self.account = account
    }

    public func load() throws -> [ManagedServer] {
        guard let data = try item.read() else { return [] }
        guard let servers = try? JSONDecoder().decode([ManagedServer].self, from: data) else {
            throw CredentialStoreError.corruptedData
        }
        return servers
    }

    public func save(_ servers: [ManagedServer]) throws {
        guard !servers.isEmpty else { return try item.delete() }
        try item.write(try JSONEncoder().encode(servers))
    }

    private var item: KeychainItem { KeychainItem(service: service, account: account) }
}

/// A `ManagedServerStore` in memory, for tests and previews.
public final class InMemoryManagedServerStore: ManagedServerStore, @unchecked Sendable {
    private let stored: Mutex<[ManagedServer]>

    public init(_ servers: [ManagedServer] = []) { stored = Mutex(servers) }

    public func load() throws -> [ManagedServer] { stored.withLock { $0 } }
    public func save(_ servers: [ManagedServer]) throws { stored.withLock { $0 = servers } }
}
