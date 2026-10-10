import Foundation

/// What the apps need from an OpenID Provider's discovery document (`/.well-known/openid-configuration`).
public struct OIDCProvider: Decodable, Sendable, Equatable {
    public var issuer: String
    public var authorizationEndpoint: URL
    public var tokenEndpoint: URL
    /// RFC 8628; `nil` when the provider has no device flow.
    public var deviceAuthorizationEndpoint: URL?
    /// RFC 7009.
    public var revocationEndpoint: URL?
    /// OpenID Connect RP-Initiated Logout: ends the provider's browser session (its cookie).
    public var endSessionEndpoint: URL?

    public init(
        issuer: String,
        authorizationEndpoint: URL,
        tokenEndpoint: URL,
        deviceAuthorizationEndpoint: URL? = nil,
        revocationEndpoint: URL? = nil,
        endSessionEndpoint: URL? = nil
    ) {
        self.issuer = issuer
        self.authorizationEndpoint = authorizationEndpoint
        self.tokenEndpoint = tokenEndpoint
        self.deviceAuthorizationEndpoint = deviceAuthorizationEndpoint
        self.revocationEndpoint = revocationEndpoint
        self.endSessionEndpoint = endSessionEndpoint
    }

    private enum CodingKeys: String, CodingKey {
        case issuer
        case authorizationEndpoint = "authorization_endpoint"
        case tokenEndpoint = "token_endpoint"
        case deviceAuthorizationEndpoint = "device_authorization_endpoint"
        case revocationEndpoint = "revocation_endpoint"
        case endSessionEndpoint = "end_session_endpoint"
    }
}

/// The tokens of the central account on this device. All tokens are secrets: `description`, `debugDescription`
/// and the mirror (`dump`) hide them. The `Codable` form (the Keychain item) keeps them.
public struct TokenSet: Codable, Sendable, Equatable {
    public var accessToken: String
    /// `nil` when the provider gave none (no `offline_access`): the session ends with the access token.
    public var refreshToken: String?
    public var expiresAt: Date
    /// The ID token of the login, kept only as `id_token_hint` for the end-session URL. Also a secret.
    public var idToken: String?

    public init(accessToken: String, refreshToken: String?, expiresAt: Date, idToken: String? = nil) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.expiresAt = expiresAt
        self.idToken = idToken
    }

    /// Whether the access token still has at least `margin` seconds to live at `now`.
    public func isFresh(at now: Date, margin: TimeInterval = 60) -> Bool {
        expiresAt.timeIntervalSince(now) >= margin
    }
}

/// One authorization request (Authorization Code with PKCE). Keep it until the callback comes back:
/// `state` and `verifier` are checked and sent then. The verifier is a secret.
public struct PKCERequest: Sendable, Equatable {
    /// The authorization URL to open in the browser session.
    public var url: URL
    public var state: String
    public var verifier: String
    public var redirectURI: URL

    public init(url: URL, state: String, verifier: String, redirectURI: URL) {
        self.url = url
        self.state = state
        self.verifier = verifier
        self.redirectURI = redirectURI
    }
}

/// A device authorization (RFC 8628 3.2). `deviceCode` is the secret of the poll and never leaves the device
/// that asked for it; `userCode` is what the user types (or the phone opens) to approve.
public struct DeviceAuthorization: Sendable, Equatable {
    public var deviceCode: String
    public var userCode: String
    public var verificationURI: URL
    public var verificationURIComplete: URL?
    public var expiresAt: Date
    public var interval: Duration

    public init(
        deviceCode: String,
        userCode: String,
        verificationURI: URL,
        verificationURIComplete: URL?,
        expiresAt: Date,
        interval: Duration
    ) {
        self.deviceCode = deviceCode
        self.userCode = userCode
        self.verificationURI = verificationURI
        self.verificationURIComplete = verificationURIComplete
        self.expiresAt = expiresAt
        self.interval = interval
    }

    /// A user code the way Zitadel shows it: surrounding spaces dropped, upper case, and `XXXX-XXXX` for
    /// eight characters without a hyphen. Anything else stays as is (upper case).
    public static func normalizedUserCode(_ code: String) -> String {
        let code = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard code.count == 8, !code.contains("-") else { return code }
        return "\(code.prefix(4))-\(code.suffix(4))"
    }
}

/// Errors of `OIDCClient`. No case carries a token, a code or a verifier.
public enum OIDCError: Error, Sendable, Equatable {
    /// The discovery document names another issuer than the one asked for, or the callback's `iss` is not the
    /// provider's issuer (RFC 9207).
    case discoveryMismatch
    /// The callback is not the answer to this request: wrong redirect URI, wrong or repeated `state`.
    case stateMismatch
    /// The callback has no `code`.
    case missingCode
    /// The callback came back with `error` (for example `access_denied`). The code only: `unknown_error` when
    /// it has characters an OAuth error code cannot have.
    case authorizationFailed(String)
    /// The code or the refresh token was refused: the session is over.
    case invalidGrant
    /// Device flow: the device code expired before the user approved.
    case expiredToken
    /// Device flow: the user denied the login.
    case accessDenied
    /// The provider has no `device_authorization_endpoint`.
    case deviceFlowUnavailable
    /// Any other `error` of the provider (`http_<status>` when the reply had none).
    case server(String)
    /// No HTTP reply at all (offline, DNS, TLS, timeout).
    case network(URLError.Code)
    /// A reply the client does not understand, or an endpoint that is not `https://`.
    case malformedResponse
}

// MARK: - Secrets stay out of descriptions

extension TokenSet: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String {
        "TokenSet(accessToken: <redacted>, refreshToken: \(refreshToken == nil ? "nil" : "<redacted>"), "
            + "expiresAt: \(expiresAt), idToken: \(idToken == nil ? "nil" : "<redacted>"))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: [
            "accessToken": "<redacted>",
            "refreshToken": refreshToken == nil ? "nil" : "<redacted>",
            "expiresAt": expiresAt,
            "idToken": idToken == nil ? "nil" : "<redacted>",
        ])
    }
}

extension PKCERequest: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "PKCERequest(redirectURI: \(redirectURI), verifier: <redacted>)" }
    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: ["redirectURI": redirectURI, "verifier": "<redacted>"])
    }
}

extension DeviceAuthorization: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String {
        "DeviceAuthorization(deviceCode: <redacted>, userCode: \(userCode), verificationURI: \(verificationURI), "
            + "expiresAt: \(expiresAt), interval: \(interval))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: [
            "deviceCode": "<redacted>",
            "userCode": userCode,
            "verificationURI": verificationURI,
            "verificationURIComplete": verificationURIComplete as Any,
            "expiresAt": expiresAt,
            "interval": interval,
        ])
    }
}
