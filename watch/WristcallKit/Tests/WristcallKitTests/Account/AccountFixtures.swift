import CryptoKit
import Foundation
import WristcallKit
import WristcallKitTesting

/// Builders shared by the account tests.
enum AccountFixtures {
    static let redirect = URL(string: "wristcall://auth/callback")!

    static func provider(
        issuer: String = "https://auth.test",
        token: URL = URL(string: "https://auth.test/oauth/v2/token")!,
        device: URL? = URL(string: "https://auth.test/oauth/v2/device_authorization")!,
        revocation: URL? = URL(string: "https://auth.test/oauth/v2/revoke")!
    ) -> OIDCProvider {
        OIDCProvider(
            issuer: issuer,
            authorizationEndpoint: URL(string: "https://auth.test/oauth/v2/authorize")!,
            tokenEndpoint: token,
            deviceAuthorizationEndpoint: device,
            revocationEndpoint: revocation,
            endSessionEndpoint: URL(string: "https://auth.test/oidc/v1/end_session")!
        )
    }

    /// The discovery document of `issuer`, with every endpoint on `endpoints` (both without trailing slash).
    static func discovery(issuer: String, endpoints: String) -> String {
        """
        {"issuer":"\(issuer)",
         "authorization_endpoint":"\(endpoints)/oauth/v2/authorize",
         "token_endpoint":"\(endpoints)/oauth/v2/token",
         "device_authorization_endpoint":"\(endpoints)/oauth/v2/device_authorization",
         "revocation_endpoint":"\(endpoints)/oauth/v2/revoke",
         "end_session_endpoint":"\(endpoints)/oidc/v1/end_session",
         "code_challenge_methods_supported":["S256"]}
        """
    }

    /// The `/v1/config` body of the E6 Cloud (`public_config`), pointing at `issuer`.
    static func config(issuer: String, ios: String? = "wristcall-ios", watch: String? = "wristcall-watch") -> String {
        var clients: [String] = []
        if let ios { clients.append(#""ios":"\#(ios)""#) }
        clients.append(#""pwa":"wristcall-pwa""#)
        if let watch { clients.append(#""watch":"\#(watch)""#) }
        return """
        {"issuer":"\(issuer)","project_id":"345678901234567890",
         "clients":{\(clients.joined(separator: ","))},
         "scopes":["openid","profile","offline_access","urn:zitadel:iam:org:project:id:345678901234567890:aud"],
         "server_tokens":true,
         "push":{"apns":true,"webpush":false,"vapid_public_key":null,
                 "apns_topics":["br.com.trigram.wristcall","br.com.trigram.wristcall.watchkitapp"]}}
        """
    }

    static func query(_ url: URL) -> [String: String] {
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(items.map { ($0.name, $0.value ?? "") }) { first, _ in first }
    }
}

/// A Cloud, its issuer and the issuer's endpoints, all stubbed. `token` answers the token endpoint and
/// `revoke` the revocation endpoint; the Cloud answers `/v1/config` with `config` and anything else with `cloudReply`.
final class StubAccountWorld: Sendable {
    let endpoints: StubHost
    let issuer: StubHost
    let cloud: StubHost

    init(
        ios: String? = "wristcall-ios",
        token: @escaping @Sendable (StubHost.Request) throws -> StubHost.Reply = { _ in
            StubHost.Reply(200, #"{"access_token":"new-at","refresh_token":"new-rt","expires_in":3600,"token_type":"Bearer"}"#)
        },
        revoke: @escaping @Sendable (StubHost.Request) throws -> StubHost.Reply = { _ in StubHost.Reply(200, "") },
        cloudReply: @escaping @Sendable (StubHost.Request) throws -> StubHost.Reply = { _ in StubHost.Reply(404, "{}") }
    ) {
        let endpoints = StubHost { request in
            request.path.hasSuffix("/revoke") ? try revoke(request) : try token(request)
        }
        let endpointsBase = endpoints.url.absoluteString
        let issuer = StubHostWithSelf { url in AccountFixtures.discovery(issuer: url, endpoints: endpointsBase) }
        let issuerURL = issuer.url.absoluteString
        self.cloud = StubHost { request in
            if request.path == "/v1/config" {
                return StubHost.Reply(200, AccountFixtures.config(issuer: issuerURL, ios: ios))
            }
            return try cloudReply(request)
        }
        self.endpoints = endpoints
        self.issuer = issuer.stub
    }

    var tokenRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/token") } }
    var revokeRequests: [StubHost.Request] { endpoints.requests.filter { $0.path.hasSuffix("/revoke") } }
}

final class IssuerBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value = ""
    var url: String {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

extension Data {
    /// Base64url without padding, written independently of the Kit's own encoder.
    func base64URLEncodedStringForTest() -> String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }
}
