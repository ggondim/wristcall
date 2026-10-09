import Foundation

/// What a shortcut asked for: `agent` is the text of an `AgentRef`, unvalidated (the app checks
/// it against the agents it knows); `nil` means the first agent.
public struct PendingCall: Sendable, Equatable {
    public var agent: String?

    public init(agent: String? = nil) {
        self.agent = agent
    }
}

/// A "call the agent now" request left by a shortcut (the App Intent, the complication, the
/// control) for the app to act on once it can call. At most one request is pending, and it
/// expires, so a request the app never picked up does not start a call later by surprise.
public struct PendingCallStore: @unchecked Sendable {
    /// Posted on `notificationCenter` by `request()`, in the process that made the request.
    public static let didRequest = Notification.Name("io.github.ggondim.wristcall.callRequested")
    public static let defaultsKey = "pendingCallRequestedAt"
    public static let agentDefaultsKey = "pendingCallAgent"
    /// A request older than this is dropped.
    public static let maxAge: TimeInterval = 30
    /// Tolerance for a clock that moved back between the request and the check.
    static let clockSkew: TimeInterval = 5

    // UserDefaults and NotificationCenter are thread-safe; hence `@unchecked Sendable`.
    private let defaults: UserDefaults
    private let notificationCenter: NotificationCenter
    private let now: @Sendable () -> Date

    public init(
        defaults: UserDefaults = .standard,
        notificationCenter: NotificationCenter = .default,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.defaults = defaults
        self.notificationCenter = notificationCenter
        self.now = now
    }

    /// Records a request (replacing an older one, and its agent) and posts `didRequest`.
    public func request(agent: String? = nil) {
        defaults.set(agent, forKey: Self.agentDefaultsKey)
        defaults.set(now().timeIntervalSince1970, forKey: Self.defaultsKey)
        notificationCenter.post(name: Self.didRequest, object: nil)
    }

    /// A fresh request is waiting. Does not consume it.
    public var isPending: Bool {
        guard let requestedAt else { return false }
        return isFresh(requestedAt)
    }

    /// The request exactly once, while fresh; `nil` otherwise. Always clears what was stored.
    public func consume() -> PendingCall? {
        guard let requestedAt else { return nil }
        let agent = defaults.string(forKey: Self.agentDefaultsKey)
        defaults.removeObject(forKey: Self.defaultsKey)
        defaults.removeObject(forKey: Self.agentDefaultsKey)
        return isFresh(requestedAt) ? PendingCall(agent: agent) : nil
    }

    private var requestedAt: TimeInterval? {
        defaults.object(forKey: Self.defaultsKey) as? TimeInterval
    }

    private func isFresh(_ requestedAt: TimeInterval) -> Bool {
        let age = now().timeIntervalSince1970 - requestedAt
        return age >= -Self.clockSkew && age <= Self.maxAge
    }
}
