import Foundation

/// The URL the complication opens (`widgetURL`). The app registers the `wristcall` scheme and
/// turns this URL into a `PendingCallStore` request.
public enum ShortcutLink {
    public static let scheme = "wristcall"
    public static let call = URL(string: "wristcall://call")!

    /// `wristcall://call`, in any letter case, with nothing else that matters.
    public static func isCall(_ url: URL) -> Bool {
        url.scheme?.lowercased() == scheme && url.host()?.lowercased() == "call"
    }
}
