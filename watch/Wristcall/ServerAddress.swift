import Foundation

/// Validates a server or directory address typed (or dictated) by the user.
enum ServerAddress {
    /// The only hosts allowed over plain `http://` (local development and the simulator).
    static let developmentHosts: Set<String> = ["localhost", "127.0.0.1"]

    /// `https://host[:port][/path]`, or `http://` for `developmentHosts` only. Surrounding spaces and
    /// trailing slashes are dropped; a text without scheme gets `https://`. Anything else is `nil`.
    static func parse(_ text: String) -> URL? {
        var text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") {
            text = "https://" + text
        }
        while text.hasSuffix("/") {
            text.removeLast()
        }
        guard let components = URLComponents(string: text),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased(), !host.isEmpty,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil
        else { return nil }
        switch scheme {
        case "https":
            break
        case "http" where developmentHosts.contains(host):
            break
        default:
            return nil
        }
        return URL(string: text)
    }
}
