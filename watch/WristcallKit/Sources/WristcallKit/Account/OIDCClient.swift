import CryptoKit
import Foundation
import Security

/// OpenID Connect public client (no secret): Authorization Code with PKCE (S256) for the phone, the device
/// authorization grant (RFC 8628) for the watch, refresh, revocation and the end-session URL.
/// `client_id` goes in the `application/x-www-form-urlencoded` body of every POST.
///
/// Nothing here logs; tokens, codes, the verifier and the device code never reach an error or a description.
public struct OIDCClient: Sendable {
    public typealias Sleep = @Sendable (Duration) async throws -> Void

    public let issuer: URL
    public let clientID: String
    private let session: URLSession
    private let sleep: Sleep
    private let now: @Sendable () -> Date
    private let timeout: TimeInterval = 15

    static let deviceCodeGrant = "urn:ietf:params:oauth:grant-type:device_code"
    /// RFC 8628 3.5: `slow_down` adds 5 seconds to the interval, for this and every later poll.
    static let slowDownStep: Duration = .seconds(5)
    /// RFC 8628 3.2: the interval when the provider names none.
    static let defaultInterval = 5
    /// A token reply without `expires_in` counts as five minutes.
    static let defaultLifetime: TimeInterval = 300

    public init(
        issuer: URL,
        clientID: String,
        session: URLSession = .shared,
        sleep: @escaping Sleep = { try await Task.sleep(for: $0) },
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.issuer = issuer
        self.clientID = clientID
        self.session = session
        self.sleep = sleep
        self.now = now
    }

    // MARK: - Discovery

    /// `GET {issuer}/.well-known/openid-configuration`. The issuer and every endpoint must be `https://`
    /// (`http://` only on `localhost`/`127.0.0.1`; a plain issuer is refused before any request), and the
    /// document must name this very issuer (trailing slashes aside).
    public func discover() async throws -> OIDCProvider {
        guard Self.isSecure(issuer) else { throw OIDCError.malformedResponse }
        var request = URLRequest(
            url: issuer.appending(path: ".well-known/openid-configuration"),
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: timeout
        )
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (status, data) = try await send(request)
        guard status == 200 else { throw OIDCError.server("http_\(status)") }
        guard let provider = try? JSONDecoder().decode(OIDCProvider.self, from: data) else {
            throw OIDCError.malformedResponse
        }
        guard Self.trimmed(provider.issuer) == Self.trimmed(issuer.absoluteString) else {
            throw OIDCError.discoveryMismatch
        }
        let endpoints = [provider.authorizationEndpoint, provider.tokenEndpoint]
            + [provider.deviceAuthorizationEndpoint, provider.revocationEndpoint, provider.endSessionEndpoint].compactMap { $0 }
        guard endpoints.allSatisfy(Self.isSecure) else { throw OIDCError.malformedResponse }
        return provider
    }

    // MARK: - Authorization Code with PKCE

    /// A new authorization request: fresh random `state` and `verifier` (32 bytes each, base64url),
    /// `code_challenge = base64url(SHA256(verifier))`, method `S256`.
    public func authorizationRequest(_ provider: OIDCProvider, redirectURI: URL, scopes: [String]) -> PKCERequest {
        let state = Self.randomToken()
        let verifier = Self.randomToken()
        let challenge = Data(SHA256.hash(data: Data(verifier.utf8))).base64URLEncodedString()
        var components = URLComponents(url: provider.authorizationEndpoint, resolvingAgainstBaseURL: false)
            ?? URLComponents()
        components.queryItems = (components.queryItems ?? []) + [
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "redirect_uri", value: redirectURI.absoluteString),
            URLQueryItem(name: "scope", value: scopes.joined(separator: " ")),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "code_challenge", value: challenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
        ]
        // URLComponents leaves "+" as is in a query, and servers decode it as a space.
        if let encoded = components.percentEncodedQuery {
            components.percentEncodedQuery = encoded.replacingOccurrences(of: "+", with: "%2B")
        }
        let url = components.url ?? provider.authorizationEndpoint
        return PKCERequest(url: url, state: state, verifier: verifier, redirectURI: redirectURI)
    }

    /// Checks the browser's callback against `request` and trades its code for tokens.
    ///
    /// The callback must have the redirect URI's scheme, host, port and path, exactly one `state` equal to the
    /// request's, and, when present, an `iss` equal to the provider's issuer (RFC 9207). Only then does an `error`
    /// count (`.authorizationFailed`) or the code go to the token endpoint, with the verifier.
    public func exchange(callback: URL, for request: PKCERequest, _ provider: OIDCProvider) async throws -> TokenSet {
        guard let components = URLComponents(url: callback, resolvingAgainstBaseURL: false),
              let expected = URLComponents(url: request.redirectURI, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == expected.scheme?.lowercased(),
              components.host?.lowercased() == expected.host?.lowercased(),
              components.port == expected.port,
              components.path == expected.path
        else { throw OIDCError.stateMismatch }
        let items = components.queryItems ?? []
        func values(_ name: String) -> [String] { items.filter { $0.name == name }.map { $0.value ?? "" } }

        let states = values("state")
        guard states.count == 1, Self.constantTimeEqual(states[0], request.state) else { throw OIDCError.stateMismatch }
        let issuers = values("iss")
        if !issuers.isEmpty {
            guard issuers.count == 1, Self.trimmed(issuers[0]) == Self.trimmed(provider.issuer) else {
                throw OIDCError.discoveryMismatch
            }
        }
        if let error = values("error").first {
            throw OIDCError.authorizationFailed(Self.errorCode(error) ?? "unknown_error")
        }
        let codes = values("code")
        guard codes.count == 1, !codes[0].isEmpty else { throw OIDCError.missingCode }

        return try await tokenRequest(provider.tokenEndpoint, [
            ("grant_type", "authorization_code"),
            ("code", codes[0]),
            ("redirect_uri", request.redirectURI.absoluteString),
            ("client_id", clientID),
            ("code_verifier", request.verifier),
        ], previousRefreshToken: nil)
    }

    // MARK: - Refresh, revocation, end session

    /// `grant_type=refresh_token`. A reply without `refresh_token` keeps `refreshToken`. `.invalidGrant`: the
    /// refresh token is revoked or expired, the session is over.
    public func refresh(_ refreshToken: String, _ provider: OIDCProvider) async throws -> TokenSet {
        try await tokenRequest(provider.tokenEndpoint, [
            ("grant_type", "refresh_token"),
            ("refresh_token", refreshToken),
            ("client_id", clientID),
        ], previousRefreshToken: refreshToken)
    }

    /// RFC 7009. Nothing to do when the provider has no revocation endpoint. Callers signing out ignore errors:
    /// the local tokens go either way.
    public func revoke(_ token: String, _ provider: OIDCProvider) async throws {
        guard let endpoint = provider.revocationEndpoint else { return }
        let (status, data) = try await send(formRequest(endpoint, [("token", token), ("client_id", clientID)]))
        guard status == 200 else { throw Self.providerError(status: status, data: data) }
    }

    /// The URL that ends the provider's browser session (its cookie would otherwise sign the same user in
    /// again). `nil` when the provider has no end-session endpoint.
    /// `client_id` always names the client; `id_token_hint` (the login's ID token, a secret: the URL must not be
    /// logged) is added when there is one.
    public func endSessionURL(_ provider: OIDCProvider, postLogoutRedirect: URL, idTokenHint: String? = nil) -> URL? {
        guard let endpoint = provider.endSessionEndpoint,
              var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)
        else { return nil }
        var items = (components.queryItems ?? []) + [
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "post_logout_redirect_uri", value: postLogoutRedirect.absoluteString),
        ]
        if let idTokenHint, !idTokenHint.isEmpty {
            items.append(URLQueryItem(name: "id_token_hint", value: idTokenHint))
        }
        components.queryItems = items
        if let encoded = components.percentEncodedQuery {
            components.percentEncodedQuery = encoded.replacingOccurrences(of: "+", with: "%2B")
        }
        return components.url
    }

    // MARK: - Device authorization grant (RFC 8628)

    /// Asks for a device code. `.deviceFlowUnavailable` when the provider has no device endpoint. The
    /// verification URIs must be `https://`; an interval under one second counts as one second. The user code
    /// is normalized (`DeviceAuthorization.normalizedUserCode`).
    public func startDeviceAuthorization(_ provider: OIDCProvider, scopes: [String]) async throws -> DeviceAuthorization {
        guard let endpoint = provider.deviceAuthorizationEndpoint else { throw OIDCError.deviceFlowUnavailable }
        let (status, data) = try await send(formRequest(endpoint, [
            ("client_id", clientID),
            ("scope", scopes.joined(separator: " ")),
        ]))
        guard status == 200 else { throw Self.providerError(status: status, data: data) }
        guard let reply = try? JSONDecoder().decode(DeviceAuthorizationReply.self, from: data),
              !reply.deviceCode.isEmpty, !reply.userCode.isEmpty,
              let verificationURI = URL(string: reply.verificationURI), Self.isSecure(verificationURI),
              reply.expiresIn > 0
        else { throw OIDCError.malformedResponse }
        let complete = reply.verificationURIComplete.flatMap(URL.init(string:)).flatMap { Self.isSecure($0) ? $0 : nil }
        return DeviceAuthorization(
            deviceCode: reply.deviceCode,
            userCode: DeviceAuthorization.normalizedUserCode(reply.userCode),
            verificationURI: verificationURI,
            verificationURIComplete: complete,
            expiresAt: now().addingTimeInterval(reply.expiresIn),
            interval: .seconds(max(reply.interval ?? Self.defaultInterval, 1))
        )
    }

    /// Polls the token endpoint until the user approves (RFC 8628 3.4 and 3.5). Waits `interval` before every
    /// poll; `authorization_pending` waits again, `slow_down` adds 5 s for good. A transient failure (no HTTP
    /// reply, or a 5xx) counts as pending: one dropped poll must not end a login the user may be approving.
    /// Ends with `.expiredToken` once the next poll would come after `expiresAt` (no request then),
    /// `.accessDenied`, any other OAuth error, or the task's `CancellationError`.
    public func pollDeviceToken(_ authorization: DeviceAuthorization, _ provider: OIDCProvider) async throws -> TokenSet {
        var interval = authorization.interval
        let fields = [
            ("grant_type", Self.deviceCodeGrant),
            ("device_code", authorization.deviceCode),
            ("client_id", clientID),
        ]
        while true {
            try Task.checkCancellation()
            guard now().addingTimeInterval(Self.seconds(interval)) <= authorization.expiresAt else {
                throw OIDCError.expiredToken
            }
            try await sleep(interval)
            try Task.checkCancellation()
            guard now() < authorization.expiresAt else { throw OIDCError.expiredToken }
            let issuedAt = now()
            let status: Int, data: Data
            do {
                (status, data) = try await send(formRequest(provider.tokenEndpoint, fields))
            } catch OIDCError.network(let code) {
                if Task.isCancelled { throw CancellationError() }
                if code == .cancelled { throw OIDCError.network(code) }
                continue
            }
            if status == 200 {
                return try tokens(from: data, issuedAt: issuedAt, previousRefreshToken: nil)
            }
            if (500...599).contains(status) { continue }
            switch Self.providerError(status: status, data: data) {
            case .server("authorization_pending"): continue
            case .server("slow_down"): interval += Self.slowDownStep
            case .server("access_denied"): throw OIDCError.accessDenied
            case .server("expired_token"): throw OIDCError.expiredToken
            case let error: throw error
            }
        }
    }

    // MARK: - Plumbing

    private func tokenRequest(
        _ endpoint: URL, _ fields: [(String, String)], previousRefreshToken: String?
    ) async throws -> TokenSet {
        let issuedAt = now()
        let (status, data) = try await send(formRequest(endpoint, fields))
        guard status == 200 else { throw Self.providerError(status: status, data: data) }
        return try tokens(from: data, issuedAt: issuedAt, previousRefreshToken: previousRefreshToken)
    }

    /// A `200` token reply as a `TokenSet`.
    private func tokens(from data: Data, issuedAt: Date, previousRefreshToken: String?) throws -> TokenSet {
        guard let reply = try? JSONDecoder().decode(TokenReply.self, from: data),
              !reply.accessToken.isEmpty,
              reply.tokenType.map({ $0.lowercased() == "bearer" }) ?? true
        else { throw OIDCError.malformedResponse }
        let refreshToken = reply.refreshToken.flatMap { $0.isEmpty ? nil : $0 } ?? previousRefreshToken
        let lifetime = max(reply.expiresIn ?? Self.defaultLifetime, 0)
        return TokenSet(
            accessToken: reply.accessToken,
            refreshToken: refreshToken,
            expiresAt: issuedAt.addingTimeInterval(lifetime),
            idToken: reply.idToken.flatMap { $0.isEmpty ? nil : $0 }
        )
    }

    private func formRequest(_ url: URL, _ fields: [(String, String)]) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(fields.map { "\(Self.formEncode($0.0))=\(Self.formEncode($0.1))" }
            .joined(separator: "&").utf8)
        return request
    }

    private func send(_ request: URLRequest) async throws -> (status: Int, data: Data) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw OIDCError.malformedResponse }
            return (http.statusCode, data)
        } catch let error as URLError {
            throw OIDCError.network(error.code)
        }
    }

    /// The `error` of a refused request: `invalid_grant` is the session's end, anything else is `.server`.
    static func providerError(status: Int, data: Data) -> OIDCError {
        let body = try? JSONDecoder().decode(ErrorReply.self, from: data)
        guard let code = body?.error.flatMap(errorCode) else { return .server("http_\(status)") }
        return code == "invalid_grant" ? .invalidGrant : .server(code)
    }

    /// An OAuth error code as is, or `nil` when it has characters no error code has (it may end up on screen).
    static func errorCode(_ text: String) -> String? {
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        guard (1...64).contains(text.count), text.allSatisfy(allowed.contains) else { return nil }
        return text
    }

    /// `https://`, or `http://` on the development hosts only.
    static func isSecure(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), let host = url.host()?.lowercased(), !host.isEmpty else {
            return false
        }
        return scheme == "https" || (scheme == "http" && ServerAddress.developmentHosts.contains(host))
    }

    static func trimmed(_ text: String) -> String {
        var text = text
        while text.hasSuffix("/") {
            text.removeLast()
        }
        return text
    }

    /// 32 random bytes from the system's secure generator, base64url without padding (43 characters).
    static func randomToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        if status != errSecSuccess {
            // Never fall back to a weak source: SystemRandomNumberGenerator is the OS CSPRNG too.
            var generator = SystemRandomNumberGenerator()
            bytes = (0..<32).map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        }
        return Data(bytes).base64URLEncodedString()
    }

    static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let a = Array(lhs.utf8), b = Array(rhs.utf8)
        guard a.count == b.count else { return false }
        return zip(a, b).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }

    static func seconds(_ duration: Duration) -> TimeInterval {
        let parts = duration.components
        return TimeInterval(parts.seconds) + TimeInterval(parts.attoseconds) / 1e18
    }

    /// `application/x-www-form-urlencoded`: everything but unreserved characters is percent-encoded.
    static func formEncode(_ text: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        // `alphanumerics` holds non-ASCII letters too: encode anything outside ASCII.
        return text.unicodeScalars.map { scalar in
            scalar.isASCII && allowed.contains(scalar)
                ? String(scalar)
                : String(scalar).utf8.map { String(format: "%%%02X", $0) }.joined()
        }.joined()
    }
}

extension Data {
    /// Base64url without padding (RFC 4648 section 5, RFC 7636 appendix A).
    func base64URLEncodedString() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}

private struct TokenReply: Decodable {
    var accessToken: String
    var refreshToken: String?
    var expiresIn: TimeInterval?
    var tokenType: String?
    var idToken: String?

    private enum CodingKeys: String, CodingKey {
        case idToken = "id_token"
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case expiresIn = "expires_in"
        case tokenType = "token_type"
    }
}

private struct DeviceAuthorizationReply: Decodable {
    var deviceCode: String
    var userCode: String
    var verificationURI: String
    var verificationURIComplete: String?
    var expiresIn: TimeInterval
    var interval: Int?

    private enum CodingKeys: String, CodingKey {
        case deviceCode = "device_code"
        case userCode = "user_code"
        case verificationURI = "verification_uri"
        case verificationURIComplete = "verification_uri_complete"
        case expiresIn = "expires_in"
        case interval
    }
}

private struct ErrorReply: Decodable {
    var error: String?
}
