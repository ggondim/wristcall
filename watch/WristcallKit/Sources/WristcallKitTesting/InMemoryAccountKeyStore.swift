import Foundation
import Synchronization
import WristcallKit

/// An `AccountKeyStore` in memory, for tests that must not touch the Keychain.
public final class InMemoryAccountKeyStore: AccountKeyStore, Sendable {
    private struct State {
        var stored: [StoredAccountKey]
        var pending: AccountKey?
    }

    private let state: Mutex<State>

    public init(_ keys: [StoredAccountKey] = [], pending: AccountKey? = nil) {
        state = Mutex(State(stored: keys, pending: pending))
    }

    public func keys() throws -> [StoredAccountKey] {
        state.withLock { $0.stored.sorted { ($0.createdAt, $0.account) < ($1.createdAt, $1.account) } }
    }

    public func save(_ key: AccountKey, account: String) throws {
        state.withLock { state in
            if state.stored.contains(where: { $0.account == account && $0.key == key }) { return }
            state.stored.removeAll { $0.account == account }
            state.stored.append(StoredAccountKey(account: account, key: key, createdAt: Date()))
        }
    }

    public func pending() throws -> AccountKey? { state.withLock { $0.pending } }
    public func savePending(_ key: AccountKey) throws { state.withLock { $0.pending = key } }
    public func clearPending() throws { state.withLock { $0.pending = nil } }
    public func delete(account: String) throws { state.withLock { $0.stored.removeAll { $0.account == account } } }
    public func deleteAll() throws { state.withLock { $0.stored.removeAll() } }
}
