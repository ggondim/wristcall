import Synchronization
import WristcallKit

/// A `DeviceSessionStore` in memory, for tests and previews.
public final class InMemoryDeviceSessionStore: DeviceSessionStore, Sendable {
    private let session: Mutex<StoredDeviceSession?>

    public init(_ session: StoredDeviceSession? = nil) {
        self.session = Mutex(session)
    }

    public func load() throws -> StoredDeviceSession? { session.withLock { $0 } }
    public func save(_ session: StoredDeviceSession) throws { self.session.withLock { $0 = session } }
    public func clear() throws { session.withLock { $0 = nil } }
}
