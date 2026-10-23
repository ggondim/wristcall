import Foundation

/// Why an app asks for a route: `call` runs a call on a node, `manage` reads and changes the account's cloud-mode
/// agents and history.
public enum RoutePurpose: String, Sendable {
    case call, manage
}

/// One node the Cloud chose, with a per-server token for it (valid for that node only). A secret: `description`
/// and the mirror hide the token.
public struct RouteTarget: Sendable, Equatable {
    public let nodeID: String
    public let url: URL
    public let region: String
    public let token: String
    public let expiresAt: Date

    public init(nodeID: String, url: URL, region: String, token: String, expiresAt: Date) {
        self.nodeID = nodeID
        self.url = url
        self.region = region
        self.token = token
        self.expiresAt = expiresAt
    }
}

extension RouteTarget: Decodable {
    private struct Node: Decodable {
        var id: String
        var url: String
        var region: String
    }

    private enum CodingKeys: String, CodingKey {
        case node, token
        case expiresAt = "expires_at"
    }

    /// `{node: {id, url, region}, token, expires_at}` (seconds since 1970). The node's URL must be `https`, or
    /// `http` for `localhost` and `127.0.0.1` (local development); anything else is `APIError.malformedResponse`.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let node = try container.decode(Node.self, forKey: .node)
        guard let url = URL(string: node.url), Self.isAllowed(url) else { throw APIError.malformedResponse }
        self.init(
            nodeID: node.id, url: url, region: node.region, token: try container.decode(String.self, forKey: .token),
            expiresAt: Date(timeIntervalSince1970: try container.decode(Double.self, forKey: .expiresAt))
        )
    }

    private static func isAllowed(_ url: URL) -> Bool {
        guard let host = url.host()?.lowercased(), !host.isEmpty else { return false }
        switch url.scheme?.lowercased() {
        case "https": return true
        case "http": return host == "localhost" || host == "127.0.0.1"
        default: return false
        }
    }
}

extension RouteTarget: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String {
        "RouteTarget(nodeID: \(nodeID), url: \(url), region: \(region), token: <redacted>, expiresAt: \(expiresAt))"
    }
    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: [
            "nodeID": nodeID, "url": url, "region": region, "token": "<redacted>", "expiresAt": expiresAt,
        ])
    }
}

/// `POST /v1/route`: the node to use and, when a second node can take the request, a fallback with its own token.
public struct CloudRoute: Sendable, Equatable, Decodable {
    public let primary: RouteTarget
    public let fallback: RouteTarget?

    public init(primary: RouteTarget, fallback: RouteTarget? = nil) {
        self.primary = primary
        self.fallback = fallback
    }

    /// The answer is the primary target's own fields (`node`, `token`, `expires_at`) plus `fallback`.
    public init(from decoder: any Decoder) throws {
        primary = try RouteTarget(from: decoder)
        fallback = try decoder.container(keyedBy: CodingKeys.self).decodeIfPresent(RouteTarget.self, forKey: .fallback)
    }

    private enum CodingKeys: String, CodingKey { case fallback }
}
