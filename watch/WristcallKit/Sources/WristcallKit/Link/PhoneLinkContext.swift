import Foundation

/// iPhone → watch application context (`updateApplicationContext`): an enroll code the watch signs in with
/// (`POST /v1/devices`), so the user never types anything on the watch. The latest context replaces the
/// previous one; `expiresAt` (Unix seconds) is when the code stops working, and a context delivered later
/// is ignored by the watch. The dictionary holds plist types only. The code is a secret: it prints as `<redacted>`.
public struct PhoneLinkContext: Equatable, Sendable {
    /// Protocol version, the `"v"` of the dictionary. Any other value is ignored.
    public static let version = 1

    public let enrollCode: String
    public let expiresAt: Double

    public init(enrollCode: String, expiresAt: Double) {
        self.enrollCode = enrollCode
        self.expiresAt = expiresAt
    }

    /// `nil` for anything malformed: another version, no or empty `enroll_code`, no `expires_at`.
    public init?(_ dictionary: [String: Any]) {
        guard WatchLinkMessage.hasVersion(dictionary),
              let code = dictionary["enroll_code"] as? String, !code.isEmpty,
              let expiresAt = WatchLinkMessage.number(dictionary["expires_at"])
        else { return nil }
        self.enrollCode = code
        self.expiresAt = expiresAt
    }

    public var dictionary: [String: Any] {
        ["v": Self.version, "enroll_code": enrollCode, "expires_at": expiresAt]
    }
}

extension PhoneLinkContext: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "PhoneLinkContext(enrollCode: <redacted>, expiresAt: \(expiresAt))" }
    public var debugDescription: String { description }
    public var customMirror: Mirror {
        Mirror(self, children: ["enrollCode": "<redacted>", "expiresAt": expiresAt])
    }
}
