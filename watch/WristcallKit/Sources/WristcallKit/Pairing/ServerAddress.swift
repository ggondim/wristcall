import Foundation

/// Validates a server or directory address typed (or dictated) by the user.
public enum ServerAddress {
    /// The only hosts allowed over plain `http://` (local development and the simulator).
    public static let developmentHosts: Set<String> = ["localhost", "127.0.0.1"]

    /// `https://host[:port][/path]`, or `http://` for `developmentHosts` only. Surrounding spaces and
    /// trailing slashes are dropped; a text without scheme gets `https://`. Anything else is `nil`.
    public static func parse(_ text: String) -> URL? {
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

    /// The form used to compare URLs (the agenda against paired servers, the `aud` of a per-server token):
    /// scheme and host lowercased, the default port (443 for `https`, 80 for `http`) dropped, no trailing
    /// slash, path kept. Same result as the server's `normalize_audience` for the URLs it accepts.
    public static func canonical(_ url: URL) -> String {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme?.lowercased(),
              let host = components.host?.lowercased()
        else { return url.absoluteString }
        var authority = host.contains(":") && !host.hasPrefix("[") ? "[\(host)]" : host
        let defaultPort = scheme == "https" ? 443 : (scheme == "http" ? 80 : nil)
        if let port = components.port, port != defaultPort {
            authority += ":\(port)"
        }
        var path = components.path
        while path.hasSuffix("/") {
            path.removeLast()
        }
        return "\(scheme)://\(authority)\(path)"
    }
}
