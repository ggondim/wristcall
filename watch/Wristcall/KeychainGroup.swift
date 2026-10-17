import Foundation

/// The Keychain access group the watch shares with the iPhone app (account keys live there; the
/// watch only reads them).
enum KeychainGroup {
    /// `Info.plist`'s `WristcallKeychainGroup`: empty or not expanded (`$(` left in it, a build
    /// without a team) means no shared group.
    static func shared(infoValue: String?) -> String? {
        guard let infoValue, !infoValue.isEmpty, !infoValue.contains("$(") else { return nil }
        return infoValue
    }
}
