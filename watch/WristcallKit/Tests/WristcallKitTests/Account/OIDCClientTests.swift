import CryptoKit
import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct OIDCClientTests {
    let redirect = AccountFixtures.redirect
    let start = Date(timeIntervalSince1970: 1_800_000_000)

    func client(_ clientID: String = "ios-client", issuer: URL = URL(string: "https://auth.test")!) -> OIDCClient {
        let start = start
        return OIDCClient(issuer: issuer, clientID: clientID, session: .stubbed(), now: { start })
    }

    // MARK: - Authorization request (PKCE)

    @Test func authorizationRequestUsesS256() throws {
        let client = OIDCClient(issuer: URL(string: "https://auth.test")!, clientID: "ios-client")
        let req = client.authorizationRequest(
            AccountFixtures.provider(), redirectURI: URL(string: "wristcall://auth/callback")!,
            scopes: ["openid", "offline_access"]
        )
        let items = Dictionary(uniqueKeysWithValues: URLComponents(url: req.url, resolvingAgainstBaseURL: false)!
            .queryItems!.map { ($0.name, $0.value!) })
        #expect(req.url.absoluteString.hasPrefix("https://auth.test/oauth/v2/authorize?"))
        #expect(items["response_type"] == "code")
        #expect(items["client_id"] == "ios-client")
        #expect(items["redirect_uri"] == "wristcall://auth/callback")
        #expect(items["code_challenge_method"] == "S256")
        let digest = SHA256.hash(data: Data(req.verifier.utf8))
        #expect(items["code_challenge"] == Data(digest).base64URLEncodedStringForTest())
        #expect(items["state"] == req.state)
        #expect(items["scope"] == "openid offline_access")
        #expect(items["code_verifier"] == nil)
        #expect(req.verifier.count >= 43)
        #expect(req.redirectURI == redirect)
    }

    @Test func stateAndVerifierAreRandomBase64URL() {
        let client = client()
        let first = client.authorizationRequest(AccountFixtures.provider(), redirectURI: redirect, scopes: ["openid"])
        let second = client.authorizationRequest(AccountFixtures.provider(), redirectURI: redirect, scopes: ["openid"])
        #expect(first.state != second.state)
        #expect(first.verifier != second.verifier)
        #expect(first.state != first.verifier)
        let alphabet = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for value in [first.state, first.verifier] {
            // 32 random bytes in base64url without padding.
            #expect(value.count == 43)
            #expect(value.allSatisfy(alphabet.contains))
        }
    }

    @Test func pkceRequestDescriptionHidesVerifier() {
        let req = client().authorizationRequest(AccountFixtures.provider(), redirectURI: redirect, scopes: ["openid"])
        for text in [String(describing: req), String(reflecting: req), dumped(req)] {
            #expect(!text.contains(req.verifier))
            #expect(text.contains("<redacted>"))
        }
    }

    // MARK: - Code exchange

    @Test func exchangeRejectsWrongState() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=other")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: callback, for: req, provider) }
        // Same length, one character off.
        let last = req.state.last == "A" ? "B" : "A"
        let close = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state.dropLast())\(last)")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: close, for: req, provider) }
        // No state at all.
        let none = URL(string: "wristcall://auth/callback?code=abc")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: none, for: req, provider) }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test(arguments: [
        "wristcall://evil/callback",
        "wristcall://auth/other",
        "evil://auth/callback",
        "https://auth/callback",
        "wristcall://auth/callback/extra",
    ])
    func exchangeRejectsOtherRedirect(base: String) async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "\(base)?code=abc&state=\(req.state)")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: callback, for: req, provider) }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test func exchangeRejectsRepeatedState() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state)&state=other")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: callback, for: req, provider) }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test func exchangeRejectsOtherIssuerParameter() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state)&iss=https%3A%2F%2Fevil.test")!
        await #expect(throws: OIDCError.discoveryMismatch) {
            try await client.exchange(callback: callback, for: req, provider)
        }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test func exchangeSendsVerifier() async throws {
        let tokenHost = StubHost(replies: [
            (200, #"{"access_token":"at","refresh_token":"rt","expires_in":3600,"token_type":"Bearer","id_token":"x"}"#),
        ])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=the%2Bcode&state=\(req.state)&iss=https%3A%2F%2Fauth.test")!
        let tokens = try await client.exchange(callback: callback, for: req, provider)
        #expect(tokens == TokenSet(accessToken: "at", refreshToken: "rt", expiresAt: start.addingTimeInterval(3600), idToken: "x"))

        let request = try #require(tokenHost.requests.first)
        #expect(tokenHost.requests.count == 1)
        #expect(request.method == "POST")
        #expect(request.headers["Content-Type"] == "application/x-www-form-urlencoded")
        #expect(request.headers["Authorization"] == nil)
        let form = try request.form()
        #expect(form == [
            "grant_type": "authorization_code",
            "code": "the+code",
            "redirect_uri": "wristcall://auth/callback",
            "client_id": "ios-client",
            "code_verifier": req.verifier,
        ])
    }

    @Test func exchangeWithoutCodeFails() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        for query in ["state=\(req.state)", "code=&state=\(req.state)"] {
            let callback = URL(string: "wristcall://auth/callback?\(query)")!
            await #expect(throws: OIDCError.missingCode) { try await client.exchange(callback: callback, for: req, provider) }
        }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test func callbackErrorIsReported() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?error=access_denied&error_description=No&state=\(req.state)")!
        await #expect(throws: OIDCError.authorizationFailed("access_denied")) {
            try await client.exchange(callback: callback, for: req, provider)
        }
        // An error code with odd characters is not passed on as is.
        let odd = URL(string: "wristcall://auth/callback?error=%3Cb%3Ehi&state=\(req.state)")!
        await #expect(throws: OIDCError.authorizationFailed("unknown_error")) {
            try await client.exchange(callback: odd, for: req, provider)
        }
        // An error with a wrong state is someone else's callback.
        let forged = URL(string: "wristcall://auth/callback?error=access_denied&state=other")!
        await #expect(throws: OIDCError.stateMismatch) { try await client.exchange(callback: forged, for: req, provider) }
        #expect(tokenHost.requests.isEmpty)
    }

    @Test(arguments: [
        (400, #"{"error":"invalid_grant","error_description":"code expired"}"#, OIDCError.invalidGrant),
        (400, #"{"error":"invalid_client"}"#, .server("invalid_client")),
        (500, "oops", .server("http_500")),
        (200, #"{"refresh_token":"rt"}"#, .malformedResponse),
        (200, #"{"access_token":""}"#, .malformedResponse),
        (200, #"{"access_token":"at","token_type":"MAC"}"#, .malformedResponse),
    ])
    func exchangeErrors(status: Int, body: String, expected: OIDCError) async throws {
        let tokenHost = StubHost(replies: [(status, body)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state)")!
        await #expect(throws: expected) { try await client.exchange(callback: callback, for: req, provider) }
    }

    @Test func exchangeWrapsNetworkFailures() async throws {
        let tokenHost = StubHost { _ in throw URLError(.notConnectedToInternet) }
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state)")!
        await #expect(throws: OIDCError.network(.notConnectedToInternet)) {
            try await client.exchange(callback: callback, for: req, provider)
        }
    }

    // MARK: - Discovery

    @Test func discoveryReadsTheEndpoints() async throws {
        // The issuer document must name the issuer it was read from; an issuer may have a path.
        let host = StubHostWithSelf(path: "/tenant") { url in
            AccountFixtures.discovery(issuer: url, endpoints: "https://auth.test")
        }
        let provider = try await client(issuer: host.url).discover()
        #expect(provider == OIDCProvider(
            issuer: host.url.absoluteString,
            authorizationEndpoint: URL(string: "https://auth.test/oauth/v2/authorize")!,
            tokenEndpoint: URL(string: "https://auth.test/oauth/v2/token")!,
            deviceAuthorizationEndpoint: URL(string: "https://auth.test/oauth/v2/device_authorization")!,
            revocationEndpoint: URL(string: "https://auth.test/oauth/v2/revoke")!,
            endSessionEndpoint: URL(string: "https://auth.test/oidc/v1/end_session")!
        ))
        let request = try #require(host.stub.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/tenant/.well-known/openid-configuration")
    }

    @Test func discoveryAcceptsATrailingSlash() async throws {
        let host = StubHostWithSelf { url in AccountFixtures.discovery(issuer: url + "/", endpoints: "https://auth.test") }
        let provider = try await client(issuer: host.url).discover()
        #expect(provider.tokenEndpoint == URL(string: "https://auth.test/oauth/v2/token")!)
    }

    @Test func discoveryRejectsOtherIssuer() async throws {
        let host = StubHost(replies: [(200, AccountFixtures.discovery(issuer: "https://evil.test", endpoints: "https://auth.test"))])
        await #expect(throws: OIDCError.discoveryMismatch) { try await client(issuer: host.url).discover() }
    }

    @Test func discoveryRejectsPlainHTTPEndpoints() async throws {
        let host = StubHostWithSelf { url in AccountFixtures.discovery(issuer: url, endpoints: "http://auth.test") }
        await #expect(throws: OIDCError.malformedResponse) { try await client(issuer: host.url).discover() }
    }

    @Test func discoveryAcceptsLoopbackEndpoints() async throws {
        let host = StubHostWithSelf { url in AccountFixtures.discovery(issuer: url, endpoints: "http://127.0.0.1:8080") }
        let provider = try await client(issuer: host.url).discover()
        #expect(provider.tokenEndpoint == URL(string: "http://127.0.0.1:8080/oauth/v2/token")!)
    }

    @Test func discoveryRefusesAPlainHTTPIssuer() async throws {
        let host = StubHostWithSelf { url in AccountFixtures.discovery(issuer: url, endpoints: "https://auth.test") }
        let plain = URL(string: host.url.absoluteString.replacingOccurrences(of: "https://", with: "http://"))!
        await #expect(throws: OIDCError.malformedResponse) { try await client(issuer: plain).discover() }
        #expect(host.stub.requests.isEmpty)
    }

    @Test(arguments: [(404, "{}", OIDCError.server("http_404")), (200, "[]", .malformedResponse)])
    func discoveryErrors(status: Int, body: String, expected: OIDCError) async throws {
        let host = StubHost(replies: [(status, body)])
        await #expect(throws: expected) { try await client(issuer: host.url).discover() }
    }

    // MARK: - Refresh, revoke, end session

    @Test func refreshKeepsOldRefreshToken() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at2","token_type":"bearer"}"#)])
        let tokens = try await client("watch").refresh("rt1", AccountFixtures.provider(token: tokenHost.url))
        // No `expires_in`: 300 s.
        #expect(tokens == TokenSet(accessToken: "at2", refreshToken: "rt1", expiresAt: start.addingTimeInterval(300)))
        let form = try #require(tokenHost.requests.first).form()
        #expect(form == ["grant_type": "refresh_token", "refresh_token": "rt1", "client_id": "watch"])
    }

    @Test func refreshTakesARotatedRefreshToken() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at2","refresh_token":"rt2","expires_in":60}"#)])
        let tokens = try await client().refresh("rt1", AccountFixtures.provider(token: tokenHost.url))
        #expect(tokens == TokenSet(accessToken: "at2", refreshToken: "rt2", expiresAt: start.addingTimeInterval(60)))
    }

    @Test func refreshInvalidGrant() async throws {
        let tokenHost = StubHost(replies: [(400, #"{"error":"invalid_grant"}"#)])
        await #expect(throws: OIDCError.invalidGrant) {
            try await client().refresh("rt1", AccountFixtures.provider(token: tokenHost.url))
        }
    }

    @Test func revokeSendsTheToken() async throws {
        let host = StubHost(replies: [(200, "")])
        try await client("ios-client").revoke("rt1", AccountFixtures.provider(revocation: host.url))
        let request = try #require(host.requests.first)
        #expect(request.method == "POST")
        #expect(try request.form() == ["token": "rt1", "client_id": "ios-client"])
    }

    @Test func revokeErrors() async throws {
        let failing = StubHost(replies: [(503, #"{"error":"temporarily_unavailable"}"#)])
        await #expect(throws: OIDCError.server("temporarily_unavailable")) {
            try await client().revoke("rt1", AccountFixtures.provider(revocation: failing.url))
        }
        let offline = StubHost { _ in throw URLError(.timedOut) }
        await #expect(throws: OIDCError.network(.timedOut)) {
            try await client().revoke("rt1", AccountFixtures.provider(revocation: offline.url))
        }
        // No revocation endpoint: nothing to do.
        try await client().revoke("rt1", AccountFixtures.provider(revocation: nil))
    }

    @Test func endSessionURLNamesTheClient() throws {
        let url = try #require(client("ios-client").endSessionURL(
            AccountFixtures.provider(), postLogoutRedirect: URL(string: "wristcall://auth/logout")!
        ))
        #expect(url.absoluteString.hasPrefix("https://auth.test/oidc/v1/end_session?"))
        #expect(AccountFixtures.query(url) == ["client_id": "ios-client", "post_logout_redirect_uri": "wristcall://auth/logout"])
        var provider = AccountFixtures.provider()
        provider.endSessionEndpoint = nil
        #expect(client().endSessionURL(provider, postLogoutRedirect: URL(string: "wristcall://auth/logout")!) == nil)
    }

    @Test func endSessionURLHintsWithTheIDToken() throws {
        let url = try #require(client("ios-client").endSessionURL(
            AccountFixtures.provider(), postLogoutRedirect: URL(string: "wristcall://auth/logout")!, idTokenHint: "the.id.token"
        ))
        #expect(AccountFixtures.query(url) == [
            "client_id": "ios-client",
            "post_logout_redirect_uri": "wristcall://auth/logout",
            "id_token_hint": "the.id.token",
        ])
    }

    @Test func exchangeKeepsTheIDToken() async throws {
        let tokenHost = StubHost(replies: [(200, #"{"access_token":"at","refresh_token":"rt","id_token":"the.id.token"}"#)])
        let provider = AccountFixtures.provider(token: tokenHost.url)
        let client = client()
        let req = client.authorizationRequest(provider, redirectURI: redirect, scopes: ["openid"])
        let callback = URL(string: "wristcall://auth/callback?code=abc&state=\(req.state)")!
        let tokens = try await client.exchange(callback: callback, for: req, provider)
        #expect(tokens.idToken == "the.id.token")
    }

    // MARK: - Secrets stay out of descriptions

    @Test func tokenSetDescriptionHidesTokens() throws {
        let tokens = TokenSet(accessToken: "secret-access", refreshToken: "secret-refresh", expiresAt: start, idToken: "secret-id")
        for text in [String(describing: tokens), String(reflecting: tokens), dumped(tokens), "\(tokens)"] {
            #expect(!text.contains("secret-access"))
            #expect(!text.contains("secret-refresh"))
            #expect(!text.contains("secret-id"))
            #expect(text.contains("<redacted>"))
        }
        // The stored form keeps them, of course.
        let data = try JSONEncoder().encode(tokens)
        #expect(try JSONDecoder().decode(TokenSet.self, from: data) == tokens)
    }

    @Test func tokenSetDecodesItemsWithoutIDToken() throws {
        // Keychain items written before the ID token was kept.
        let old = #"{"accessToken":"a","refreshToken":"r","expiresAt":0}"#
        let tokens = try JSONDecoder().decode(TokenSet.self, from: Data(old.utf8))
        #expect(tokens == TokenSet(accessToken: "a", refreshToken: "r", expiresAt: Date(timeIntervalSinceReferenceDate: 0)))
        #expect(tokens.idToken == nil)
    }

    @Test func tokenSetFreshness() {
        let tokens = TokenSet(accessToken: "a", refreshToken: nil, expiresAt: start.addingTimeInterval(120))
        #expect(tokens.isFresh(at: start))
        #expect(tokens.isFresh(at: start.addingTimeInterval(60)))
        #expect(!tokens.isFresh(at: start.addingTimeInterval(61)))
        #expect(tokens.isFresh(at: start.addingTimeInterval(100), margin: 10))
        #expect(!tokens.isFresh(at: start.addingTimeInterval(200), margin: 0))
    }
}

/// `dump` output, which walks the mirror rather than `description`.
func dumped(_ value: Any) -> String {
    var text = ""
    dump(value, to: &text)
    return text
}

/// A stub host whose reply can name the host's own URL (an issuer document names its issuer).
final class StubHostWithSelf: Sendable {
    let stub: StubHost
    var url: URL { stub.url }

    init(path: String = "", _ body: @escaping @Sendable (String) -> String) {
        let box = IssuerBox()
        stub = StubHost(path: path) { _ in StubHost.Reply(200, body(box.url)) }
        box.url = stub.url.absoluteString
    }
}
