import Foundation

/// `GET /v1/health`, only what the watch needs. A 0.5.0 server has no `push`: `relay` is `nil`.
public struct ServerHealth: Decodable, Sendable, Equatable {
    public var version: String
    /// `push.relay`: the push relay URL this server is configured with. The watch compares it
    /// with its own relay URL and never trusts it by itself.
    public var relay: URL?

    public init(version: String, relay: URL? = nil) {
        self.version = version
        self.relay = relay
    }

    private enum CodingKeys: String, CodingKey {
        case version, push
    }

    private struct Push: Decodable {
        var relay: String?
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        version = try container.decode(String.self, forKey: .version)
        // `push` is absent (0.5.0) or null (push off): no relay either way.
        relay = try container.decodeIfPresent(Push.self, forKey: .push)?.relay.flatMap(URL.init(string:))
    }
}
