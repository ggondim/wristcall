import Foundation

/// Owns the Cloud device session of this device: keeps the access token fresh (one refresh at a time, because
/// the refresh token is single use and a second use signs the device out) and asks the Cloud for per-server
/// tokens. Unlike `AccountSession` it needs no Zitadel login.
public actor DeviceSession: CloudAccess {
    public nonisolated let cloudClient: CloudClient

    private let store: any DeviceSessionStore
    private let now: @Sendable () -> Date

    /// The refresh in flight; parallel callers wait for it instead of starting their own.
    private var refreshing: (id: Int, task: Task<StoredDeviceSession, any Error>)?
    private var nextRefreshID = 0
    /// Bumped by `start`, `signOut` and a refused refresh: a refresh that started before must neither write over
    /// their result nor hand its tokens to anyone.
    private var generation = 0
    /// A refreshed pair the store could not save (a Keychain write failed). It wins over the store, so the
    /// rotated refresh token is not lost; every read tries the save again.
    private var unsaved: StoredDeviceSession?

    public init(
        cloud: URL,
        store: any DeviceSessionStore,
        session: URLSession = .shared,
        now: @escaping @Sendable () -> Date = Date.init,
        client: ClientIdentity? = nil
    ) {
        self.store = store
        self.now = now
        self.cloudClient = CloudClient(cloud: cloud, session: session, client: client)
    }

    public var accountID: String? { (try? currentSession())?.account }
    public var deviceID: String? { (try? currentSession())?.device }

    /// Whether there is a session (an unreadable store counts as signed out).
    public var isSignedIn: Bool { unsaved != nil || ((try? store.load()) ?? nil) != nil }

    /// Stores the session of a new account or device. A refresh still in flight will neither write over it nor
    /// hand its result to anyone: its waiters read the new session instead.
    public func start(_ tokens: SessionTokens) throws {
        generation += 1
        refreshing?.task.cancel()
        refreshing = nil
        unsaved = nil
        try store.save(StoredDeviceSession(tokens))
    }

    /// An access token with at least 60 s to live. Refreshes when needed, one refresh at a time (parallel calls
    /// wait for the same one). A refused refresh token (`revoked`, `unauthorized`) clears the session and throws
    /// `.signedOut`; a network failure keeps it.
    public func accessToken() async throws -> String {
        while true {
            if let refreshing {
                switch await refreshing.task.result {
                case .success(let session): return session.accessToken
                case .failure(let error) where error is Superseded: continue
                case .failure(let error): throw error
                }
            }
            guard let session = try currentSession() else { throw AccountError.signedOut }
            if session.isFresh(at: now()) { return session.accessToken }
            nextRefreshID += 1
            let id = nextRefreshID
            let generation = generation
            let task = Task { try await self.refresh(session.refreshToken, generation: generation) }
            refreshing = (id, task)
            let result = await task.result
            if refreshing?.id == id { refreshing = nil }
            switch result {
            case .success(let session): return session.accessToken
            case .failure(let error) where error is Superseded: continue
            case .failure(let error): throw error
            }
        }
    }

    /// Forgets the session, in memory and in the store. The Cloud keeps the device until it is revoked there.
    public func signOut() throws {
        generation += 1
        refreshing?.task.cancel()
        refreshing = nil
        unsaved = nil
        try store.clear()
    }

    // MARK: - Refresh

    /// The session in use: the unsaved one first (trying to save it again), then the store's.
    private func currentSession() throws -> StoredDeviceSession? {
        guard let unsaved else { return try store.load() }
        if (try? store.save(unsaved)) != nil {
            self.unsaved = nil
        }
        return unsaved
    }

    private func refresh(_ refreshToken: String, generation: Int) async throws -> StoredDeviceSession {
        do {
            let tokens = try await cloudClient.refreshSession(refreshToken)
            guard generation == self.generation else { throw Superseded() }
            let session = StoredDeviceSession(tokens)
            // The new pair is saved before anyone gets its token. A failed save keeps it in memory: the old
            // refresh token is spent, so it must never be used again.
            do {
                try store.save(session)
                unsaved = nil
            } catch {
                unsaved = session
            }
            return session
        } catch let error as Superseded {
            throw error
        } catch {
            guard generation == self.generation else { throw Superseded() }
            if let error = error as? APIError, error == .revoked || error == .unauthorized {
                try? store.clear()
                unsaved = nil
                self.generation += 1
                throw AccountError.signedOut
            }
            throw error
        }
    }
}

/// A refresh outlived by `start` or `signOut`: its result is nobody's.
private struct Superseded: Error {}
