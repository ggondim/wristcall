import Foundation

/// Whether the welcome and plan screens were gone through on this iPhone (UserDefaults `onboarding.v3.done`).
/// Deleting the account resets it: the app opens on the welcome screen again.
struct OnboardingStore {
    static let key = "onboarding.v3.done"
    /// Where it is kept; tests pass their own (or use the `in:` forms).
    nonisolated(unsafe) static var defaults: UserDefaults = .standard

    static var isDone: Bool { isDone(in: defaults) }
    static func finish() { finish(in: defaults) }
    static func reset() { reset(in: defaults) }

    static func isDone(in defaults: UserDefaults) -> Bool { defaults.bool(forKey: key) }
    static func finish(in defaults: UserDefaults) { defaults.set(true, forKey: key) }
    static func reset(in defaults: UserDefaults) { defaults.removeObject(forKey: key) }
}
