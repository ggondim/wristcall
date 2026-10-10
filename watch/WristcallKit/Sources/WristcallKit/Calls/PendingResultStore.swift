import Foundation

/// A one-way call whose final status the watch has not seen yet.
public struct PendingResult: Codable, Sendable, Equatable {
    public var callID: String
    /// `Credentials.id`: the server's local id on this watch.
    public var serverID: String
    public var agentID: String?
    public var startedAt: Date

    public init(callID: String, serverID: String, agentID: String? = nil, startedAt: Date) {
        self.callID = callID
        self.serverID = serverID
        self.agentID = agentID
        self.startedAt = startedAt
    }
}

/// One-way calls without a final status yet, newest last, at most 10. Kept in `UserDefaults` so
/// that quitting the app does not lose the result (decision W10). Ids only, never text.
public struct PendingResultStore: @unchecked Sendable {
    public static let maxCount = 10

    // UserDefaults is thread-safe; hence `@unchecked Sendable`.
    private let defaults: UserDefaults
    private let key: String
    private let now: @Sendable () -> Date

    public init(
        defaults: UserDefaults = .standard,
        key: String = "wristcall.pendingResults",
        now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.defaults = defaults
        self.key = key
        self.now = now
    }

    /// Appends `result` as the newest (replacing an entry for the same call); the oldest beyond
    /// ten are dropped.
    public func add(_ result: PendingResult) {
        var all = load().filter { $0.callID != result.callID }
        all.append(result)
        save(Array(all.suffix(Self.maxCount)))
    }

    public func remove(callID: String) {
        let all = load()
        let remaining = all.filter { $0.callID != callID }
        if remaining.count != all.count { save(remaining) }
    }

    /// The newest entry younger than `maxAge` (24 h); older ones are dropped.
    public func latest(maxAge: TimeInterval = 86_400) -> PendingResult? {
        let all = load()
        let fresh = all.filter { now().timeIntervalSince($0.startedAt) <= maxAge }
        if fresh.count != all.count { save(fresh) }
        return fresh.last
    }

    // A value that does not decode is the same as no entries: it is replaced on the next write.
    private func load() -> [PendingResult] {
        guard let data = defaults.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([PendingResult].self, from: data)) ?? []
    }

    private func save(_ results: [PendingResult]) {
        if results.isEmpty {
            defaults.removeObject(forKey: key)
        } else if let data = try? JSONEncoder().encode(results) {
            defaults.set(data, forKey: key)
        }
    }
}
