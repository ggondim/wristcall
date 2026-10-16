import Foundation

/// What a wristcall link URL asks the app to do. Two shapes, each in an `https` form (the web app's page, which a
/// phone camera always opens) and in a `wristcall://` form (same fragment), with everything in the fragment:
///
///     https://wristcall-app.trigram.com.br/link#v=1&s=<server URL>&c=<link code>
///     wristcall://link#v=1&s=<server URL>&c=<link code>
///     https://wristcall-app.trigram.com.br/approve#w=<session id>&k=<key>
///     wristcall://approve#w=<session id>&k=<key>
public enum WristcallLink: Sendable, Equatable {
    /// Link the person's Cloud account to a user of `url` (always `https`) with a single-use `code`.
    case server(url: URL, code: LinkCode)
    /// Approve a web app sign-in session (`id` is `ws_` and 16 lowercase hex digits).
    case webSession(id: String, key: String)

    /// The web app's host, the only one whose `https` links are accepted.
    public static let webHost = "wristcall-app.trigram.com.br"
    /// The app's URL scheme.
    public static let scheme = "wristcall"

    /// The link `url` stands for, or `nil` if it is not a well formed wristcall link: another host or scheme,
    /// another path or form, parameters in the query instead of the fragment, a repeated key, an invalid `%` escape, a
    /// version other than 1, or a server address that is not `https`.
    public static func parse(_ url: URL) -> WristcallLink? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.user == nil, components.password == nil, components.port == nil,
              components.percentEncodedQuery == nil,
              let kind = kind(of: components),
              let fragment = components.percentEncodedFragment,
              let parameters = split(fragment)
        else { return nil }
        switch kind {
        case .link:
            return serverLink(parameters)
        case .approve:
            return webSessionLink(parameters)
        }
    }

    // MARK: - Where

    private enum Kind: String {
        case link
        case approve
    }

    private static func kind(of components: URLComponents) -> Kind? {
        guard let urlScheme = components.scheme?.lowercased() else { return nil }
        let host = components.host?.lowercased() ?? ""
        let path = components.path
        switch urlScheme {
        case "https":
            guard host == webHost, path.hasPrefix("/") else { return nil }
            return Kind(rawValue: String(path.dropFirst()))
        case scheme:
            // Host form only: `wristcall://link#…`. `wristcall:/link`, `wristcall:///link` and `wristcall:link`
            // are refused, like a trailing slash or a trailing dot in the host.
            guard path.isEmpty else { return nil }
            return Kind(rawValue: host)
        default:
            return nil
        }
    }

    // MARK: - Fragment

    /// `key=value` pairs on `&`, split at the first `=`, values percent-decoded (`+` stays a plus). `nil` for an
    /// empty fragment, an empty segment (a leading, doubled or trailing `&`), a pair without `=` or with an empty
    /// key, a repeated key, or a value that does not decode (an invalid `%` escape or invalid UTF-8 such as `%FF`).
    /// Not `URLComponents.percentEncodedQuery = fragment`: that traps on an invalid escape such as `c=%`.
    private static func split(_ fragment: String) -> [String: String]? {
        guard !fragment.isEmpty else { return nil }
        var parameters: [String: String] = [:]
        for pair in fragment.split(separator: "&", omittingEmptySubsequences: false) {
            guard let equals = pair.firstIndex(of: "=") else { return nil }
            let key = String(pair[pair.startIndex..<equals])
            guard !key.isEmpty, parameters[key] == nil,
                  let value = String(pair[pair.index(after: equals)...]).removingPercentEncoding
            else { return nil }
            parameters[key] = value
        }
        return parameters
    }

    // MARK: - What

    private static func serverLink(_ parameters: [String: String]) -> WristcallLink? {
        guard parameters["v"] == "1",
              let address = parameters["s"], let url = serverURL(address),
              let raw = parameters["c"], let code = LinkCode(raw)
        else { return nil }
        return .server(url: url, code: code)
    }

    /// `https://` only (the Cloud refuses loopback audiences), no surrounding spaces, no scheme guessing.
    private static func serverURL(_ address: String) -> URL? {
        guard address.lowercased().hasPrefix("https://"),
              !address.contains(where: \.isWhitespace),
              let url = ServerAddress.parse(address),
              url.scheme?.lowercased() == "https"
        else { return nil }
        return url
    }

    private static func webSessionLink(_ parameters: [String: String]) -> WristcallLink? {
        guard let id = parameters["w"], isSessionID(id),
              let key = parameters["k"], isSessionKey(key)
        else { return nil }
        return .webSession(id: id, key: key)
    }

    /// `ws_` followed by 16 lowercase hex digits (the Cloud's session id).
    private static func isSessionID(_ text: String) -> Bool {
        guard text.hasPrefix("ws_") else { return false }
        let hex = text.dropFirst(3)
        return hex.utf8.count == 16 && hex.utf8.allSatisfy { ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x61 && $0 <= 0x66) }
    }

    /// 1 to 128 characters of `A-Z a-z 0-9 _ -`.
    private static func isSessionKey(_ text: String) -> Bool {
        let count = text.utf8.count
        guard (1...128).contains(count), text.unicodeScalars.count == count else { return false }
        return text.utf8.allSatisfy {
            ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x41 && $0 <= 0x5A) || ($0 >= 0x61 && $0 <= 0x7A) || $0 == 0x5F || $0 == 0x2D
        }
    }
}
