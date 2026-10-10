import Foundation

/// A message between the iPhone app and the watch app over WatchConnectivity (decision R10). The
/// dictionaries hold plist types only (what `sendMessage` and `transferUserInfo` carry). Tokens never
/// cross: the iPhone sends a pairing code, the watch pairs with it as if it had been typed.
public enum WatchLinkMessage: Sendable, Equatable {
    /// iPhone → watch: pair with `server` using `code` (flow A'). `server` is the address the iPhone
    /// reaches the server at (`ManagedServer.url`); `name` is what the iPhone calls the server;
    /// `expiresAt` (Unix seconds) is when the code stops working: a queued transfer delivered later
    /// is ignored by the watch.
    case pair(server: URL, code: PairingCode, name: String, expiresAt: Double)
    /// iPhone → watch: ask every server for its agents again (decision R16).
    case refresh
    /// Watch → iPhone: the user code of the account login (RFC 8628), shown until `expiresAt` (Unix
    /// seconds). Never the `device_code`: that one is the secret of the watch's poll. Case does not
    /// matter: it is sent and compared uppercased.
    case deviceCode(userCode: String, expiresAt: Double)
    /// Watch → iPhone: the account login on the watch is done (M13): the iPhone closes the approval page it
    /// opened, which never comes back to the app on its own. Carries nothing.
    case signedIn

    /// Protocol version, the `"v"` of every dictionary. Any other value is ignored.
    public static let version = 1
    /// Longest `name` kept; longer names are cut.
    public static let maxNameLength = 64

    /// `nil` for anything malformed: another version, an unknown `type`, a server address
    /// `ServerAddress.parse` rejects (only `https://`, or `http://` on loopback), a code that is not
    /// 8 digits, an empty name, or a user code other than `XXXX-XXXX` / `XXXXXXXX` (letters and
    /// digits; lowercase is uppercased).
    public init?(_ dictionary: [String: Any]) {
        guard Self.hasVersion(dictionary), let type = dictionary["type"] as? String else { return nil }
        switch type {
        case "pair":
            guard let text = dictionary["server_url"] as? String,
                  let server = ServerAddress.parse(text),
                  let raw = dictionary["code"] as? String,
                  let code = PairingCode(raw),
                  let name = (dictionary["name"] as? String).flatMap(Self.cleanName),
                  let expiresAt = Self.number(dictionary["expires_at"])
            else { return nil }
            self = .pair(server: server, code: code, name: name, expiresAt: expiresAt)
        case "refresh":
            self = .refresh
        case "device_code":
            guard let raw = dictionary["user_code"] as? String,
                  let userCode = Self.normalizeUserCode(raw),
                  let expiresAt = Self.number(dictionary["expires_at"])
            else { return nil }
            self = .deviceCode(userCode: userCode, expiresAt: expiresAt)
        case "signed_in":
            self = .signedIn
        default:
            return nil
        }
    }

    public var dictionary: [String: Any] {
        switch self {
        case .pair(let server, let code, let name, let expiresAt):
            [
                "v": Self.version, "type": "pair", "server_url": server.absoluteString,
                "code": code.digits, "name": String(name.prefix(Self.maxNameLength)), "expires_at": expiresAt,
            ]
        case .refresh:
            ["v": Self.version, "type": "refresh"]
        case .deviceCode(let userCode, let expiresAt):
            ["v": Self.version, "type": "device_code", "user_code": userCode.uppercased(), "expires_at": expiresAt]
        case .signedIn:
            ["v": Self.version, "type": "signed_in"]
        }
    }

    public static func == (lhs: WatchLinkMessage, rhs: WatchLinkMessage) -> Bool {
        switch (lhs, rhs) {
        case let (.pair(a, b, c, d), .pair(e, f, g, h)):
            a == e && b == f && c == g && d == h
        case (.refresh, .refresh), (.signedIn, .signedIn):
            true
        case let (.deviceCode(a, b), .deviceCode(c, d)):
            a.uppercased() == c.uppercased() && b == d
        default:
            false
        }
    }

    /// `XXXX-XXXX` or `XXXXXXXX` in ASCII letters and digits, uppercased; anything else `nil`.
    public static func normalizeUserCode(_ raw: String) -> String? {
        let code = raw.uppercased()
        let groups = code.split(separator: "-", omittingEmptySubsequences: false)
        let valid: Bool
        switch groups.count {
        case 1: valid = code.count == 8
        case 2: valid = groups[0].count == 4 && groups[1].count == 4
        default: valid = false
        }
        guard valid, code.allSatisfy({ $0 == "-" || (($0.isASCII) && ($0.isLetter || $0.isNumber)) }) else { return nil }
        return code
    }

    static func hasVersion(_ dictionary: [String: Any]) -> Bool {
        (dictionary["v"] as? Int) == version
    }

    /// A plist number: `Double`, or `Int` (a Swift dictionary built in place, not bridged).
    static func number(_ value: Any?) -> Double? {
        if let value = value as? Double { return value }
        if let value = value as? Int { return Double(value) }
        return nil
    }

    private static func cleanName(_ raw: String) -> String? {
        let name = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return nil }
        return String(name.prefix(maxNameLength))
    }
}

/// The watch's answer to a `sendMessage` (`pair` or `refresh`). `error` is a short reason: one of
/// `Reason`, or the text the watch showed (a pairing failure). `pending`: the server asked for the
/// owner's approval (`requestId` is the 4 digits the watch shows); the watch keeps waiting and the
/// outcome shows up in the `applicationContext`.
public struct WatchLinkReply: Sendable, Equatable {
    public var ok: Bool
    public var error: String?
    public var pending: Bool
    public var requestId: String?

    /// Reasons the watch gives without trying.
    public enum Reason {
        /// A call is on (or the watch is in the middle of something else): try after it.
        public static let busy = "busy"
        /// The message was malformed (`WatchLinkMessage.init` returned `nil`).
        public static let invalid = "invalid"
        /// A valid message this side does not take (a `device_code` or `signed_in` sent to the watch).
        public static let unsupported = "unsupported"
        /// The pairing code expired before the watch got it.
        public static let expired = "expired"
    }

    public init(ok: Bool, error: String? = nil, pending: Bool = false, requestId: String? = nil) {
        self.ok = ok
        self.error = error
        self.pending = pending
        self.requestId = requestId
    }

    public init?(_ dictionary: [String: Any]) {
        guard let ok = dictionary["ok"] as? Bool else { return nil }
        self.ok = ok
        error = dictionary["error"] as? String
        pending = dictionary["pending"] as? Bool ?? false
        requestId = (dictionary["request_id"] as? String).flatMap { id in
            id.count == 4 && id.allSatisfy { $0.isASCII && $0.isNumber } ? id : nil
        }
    }

    public var dictionary: [String: Any] {
        var dictionary: [String: Any] = ["ok": ok]
        if let error { dictionary["error"] = error }
        if pending { dictionary["pending"] = true }
        if let requestId { dictionary["request_id"] = requestId }
        return dictionary
    }
}

/// The watch's `applicationContext`: the servers it is paired with, as `ServerAddress.canonical`
/// strings (no token, no device id). The iPhone shows "On watch" for these.
public struct WatchLinkContext: Sendable, Equatable {
    public var servers: [String]

    public init(servers: [String]) {
        self.servers = servers
    }

    public init?(_ dictionary: [String: Any]) {
        guard WatchLinkMessage.hasVersion(dictionary), let servers = dictionary["servers"] as? [String] else { return nil }
        self.servers = servers
    }

    public var dictionary: [String: Any] {
        ["v": WatchLinkMessage.version, "servers": servers]
    }
}
