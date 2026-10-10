import Foundation

/// `GET /v1/health`, only what the apps need. A 0.5.0 server has no `push`: `relay` is `nil`.
public struct ServerHealth: Decodable, Sendable, Equatable {
    public var version: String
    /// `push.relay`: the push relay URL this server is configured with. The watch compares it
    /// with its own relay URL and never trusts it by itself.
    public var relay: URL?

    /// `account`: the central account the server accepts (absent or `null` on a server without one, and
    /// when the member is malformed).
    public var account: AccountInfo?

    public struct AccountInfo: Decodable, Sendable, Equatable {
        public var issuer: String
        /// `approval` (a login on the watch waits for the owner's approval) or `attestation` (the watch is paired at once).
        public var deviceCredential: String

        public init(issuer: String, deviceCredential: String) {
            self.issuer = issuer
            self.deviceCredential = deviceCredential
        }

        private enum CodingKeys: String, CodingKey {
            case issuer
            case deviceCredential = "device_credential"
        }
    }

    public init(version: String, relay: URL? = nil, account: AccountInfo? = nil) {
        self.version = version
        self.relay = relay
        self.account = account
    }

    private enum CodingKeys: String, CodingKey { case version, push, account }

    private struct Push: Decodable { var relay: String? }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        version = try container.decode(String.self, forKey: .version)
        // `push` is absent (0.5.0) or null (push off): no relay either way.
        relay = try container.decodeIfPresent(Push.self, forKey: .push)?.relay.flatMap(URL.init(string:))
        // Absent (0.5.0) or null: no account. A member the app cannot read counts as none, too.
        account = (try? container.decodeIfPresent(AccountInfo.self, forKey: .account)) ?? nil
    }
}
