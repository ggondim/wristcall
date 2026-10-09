import Foundation

/// One agent as the widget extension and App Intents see it: enough to show and to name it,
/// never a token or a URL.
public struct CatalogAgent: Codable, Sendable, Hashable, Identifiable {
    public var ref: AgentRef
    public var slug: String
    public var displayName: String
    public var icon: String
    /// The `call_type` of the wire (`"conversation"`, `"one_way"`), not the `CallType` enum, so a
    /// type this build does not know survives a round trip.
    public var callType: String
    public var serverHost: String

    public var id: String { ref.description }

    public init(ref: AgentRef, slug: String, displayName: String, icon: String, callType: String, serverHost: String) {
        self.ref = ref
        self.slug = slug
        self.displayName = displayName
        self.icon = icon
        self.callType = callType
        self.serverHost = serverHost
    }
}

/// The agents of every paired server, shared between the app (writes) and the widget extension
/// and App Intents (read) through the App Group `UserDefaults`.
public struct AgentCatalog: @unchecked Sendable {
    public static let defaultsKey = "agentCatalog.v1"
    /// Info.plist key holding the App Group identifier (set by the build, so it follows the team).
    public static let appGroupInfoKey = "WristcallAppGroup"

    // UserDefaults is thread-safe; hence `@unchecked Sendable`.
    private let defaults: UserDefaults

    public init(defaults: UserDefaults) {
        self.defaults = defaults
    }

    /// The App Group catalog named in the Info.plist of `bundle`. Without the group (a build signed
    /// without the capability) it falls back to the standard defaults, which the widget extension
    /// does not share: it then sees an empty catalog.
    public static func shared(bundle: Bundle = .main) -> AgentCatalog {
        let group = bundle.object(forInfoDictionaryKey: appGroupInfoKey) as? String
        let defaults = group.flatMap { UserDefaults(suiteName: $0) } ?? .standard
        return AgentCatalog(defaults: defaults)
    }

    /// Empty when nothing was saved or the stored data is not a valid catalog.
    public func load() -> [CatalogAgent] {
        guard let data = defaults.data(forKey: Self.defaultsKey) else { return [] }
        return (try? JSONDecoder().decode([CatalogAgent].self, from: data)) ?? []
    }

    public func save(_ agents: [CatalogAgent]) {
        guard let data = try? JSONEncoder().encode(agents) else { return }
        defaults.set(data, forKey: Self.defaultsKey)
    }
}
