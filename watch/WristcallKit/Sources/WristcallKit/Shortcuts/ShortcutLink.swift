import Foundation

/// The URL the complication opens (`widgetURL`). The app registers the `wristcall` scheme and
/// turns this URL into a `PendingCallStore` request.
public enum ShortcutLink {
    public static let scheme = "wristcall"
    public static let call = URL(string: "wristcall://call")!
    /// Opens the app and asks for nothing: a complication with no agent chosen yet (decision W20).
    public static let open = URL(string: "wristcall://open")!

    /// `wristcall://call?agent=<text>`, with the text of an `AgentRef` percent-escaped; `nil` is the
    /// plain `call` link (first agent).
    public static func call(agent: String?) -> URL {
        guard let agent else { return call }
        var components = URLComponents()
        components.scheme = scheme
        components.host = "call"
        components.queryItems = [URLQueryItem(name: "agent", value: agent)]
        return components.url ?? call
    }

    /// The raw `agent` parameter of a call link, unvalidated; `nil` when absent or empty.
    public static func agent(in url: URL) -> String? {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
        let value = items?.first { $0.name.lowercased() == "agent" }?.value
        return value?.isEmpty == false ? value : nil
    }

    /// `wristcall://call`, in any letter case, with nothing else that matters.
    public static func isCall(_ url: URL) -> Bool {
        url.scheme?.lowercased() == scheme && url.host()?.lowercased() == "call"
    }
}
