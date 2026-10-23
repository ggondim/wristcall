import Foundation

/// Who is calling the Cloud: the platform and the product version of the app, sent on every Cloud request as
/// `X-Wristcall-Client: watchos/0.6.0`. The Cloud decides from it whether this client may use the Cloud entry.
public struct ClientIdentity: Sendable, Equatable {
    public static let headerName = "X-Wristcall-Client"
    /// The Info.plist key with the product version (the iPhone app and the embedded watch app share
    /// `CFBundleShortVersionString`, so the product version lives in its own key).
    public static let bundleKey = "WristcallClientVersion"

    public let platform: DevicePlatform
    public let version: String

    /// `nil` unless `version` is `major.minor.patch`, each part 1 to 4 digits and nothing else: the Cloud refuses
    /// any other form.
    public init?(platform: DevicePlatform, version: String) {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 3,
              parts.allSatisfy({ (1...4).contains($0.count) && $0.allSatisfy { $0.isASCII && $0.isNumber } })
        else { return nil }
        self.platform = platform
        self.version = version
    }

    /// The header value, `watchos/0.6.0`.
    public var header: String { "\(platform.rawValue)/\(version)" }

    /// Reads the version from `bundle`'s `WristcallClientVersion`; `nil` when the key is missing or invalid.
    public static func fromBundle(_ bundle: Bundle = .main, platform: DevicePlatform) -> ClientIdentity? {
        guard let version = bundle.object(forInfoDictionaryKey: bundleKey) as? String else { return nil }
        return ClientIdentity(platform: platform, version: version)
    }
}
