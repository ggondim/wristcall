import Foundation
import WristcallKit

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
    /// Writes `false`, not a removal: a removal lets a `true` from a domain below the app's own show through (on the
    /// simulator, `simctl spawn … defaults write` puts one in the device's preferences, which the app reads but
    /// never writes), and then the welcome never comes back.
    static func reset(in defaults: UserDefaults) { defaults.set(false, forKey: key) }
}

/// The screens before the tabs: the welcome, then the plan choice.
enum OnboardingStep: Equatable {
    case welcome, plan
}

/// What the plan screen ended with.
enum PlanChoice: Equatable {
    /// An invite was redeemed (or the account had a plan already): the account's plan.
    case cloud(Plan)
    /// The user runs a server: the app opens the way to connect it.
    case ownServer
    case notNow
}

/// When the root shows the onboarding and when the restore screen.
enum OnboardingGate {
    /// The step the app opens on, or `nil` for the tabs. The welcome shows on a fresh install only: not once
    /// it was gone through, not while the servers are still being read, and not to someone who already has a
    /// server or came from 0.3.x (those go straight to the agents).
    static func entryStep(isDone: Bool, serversLoaded: Bool, hasServers: Bool, didMigrate: Bool) -> OnboardingStep? {
        guard serversLoaded, !isDone, !hasServers, !didMigrate else { return nil }
        return .welcome
    }

    /// Someone who already uses the app is not shown the welcome: it counts as gone through.
    static func shouldFinishSilently(isDone: Bool, serversLoaded: Bool, hasServers: Bool, didMigrate: Bool) -> Bool {
        !isDone && serversLoaded && (hasServers || didMigrate)
    }

    /// Any way out of the plan screen ends the onboarding. Returns whether the app now opens the way to
    /// connect a server (`.ownServer`).
    @discardableResult
    static func finish(with choice: PlanChoice, in defaults: UserDefaults = OnboardingStore.defaults) -> Bool {
        OnboardingStore.finish(in: defaults)
        return choice == .ownServer
    }

    /// The restore screen covers the app while this iPhone has no way into its account.
    static func showsRestore(for state: AccountModel.State) -> Bool { state == .needsSignIn }
}
