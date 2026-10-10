import Foundation

/// Which app signs in: picks the OIDC client of `/v1/config` (`clients.ios` or `clients.watch`).
public enum AccountClientKind: String, Sendable {
    case ios, watch
}

/// Errors of `AccountSession` (the clients' own errors, `APIError` and `OIDCError`, pass through).
public enum AccountError: Error, Sendable, Equatable {
    /// The Cloud has no OIDC client for this app, or names an issuer that is not `https://`.
    case notConfigured
    /// No tokens, or the refresh token was refused (`invalid_grant`): sign in again.
    case signedOut
    /// The server accepts another central account than this Cloud (its `account.issuer`).
    case foreignIssuer(String)
    /// The server has no central account (`account` absent in `/v1/health`).
    case serverWithoutAccount
}

/// Owns the central account's tokens on one device: reads the Cloud's config, keeps the access token fresh
/// (one refresh at a time), asks the Cloud for per-server tokens and signs out.
public actor AccountSession {
    public nonisolated let cloudClient: CloudClient
    public nonisolated let cloud: URL
    public nonisolated let kind: AccountClientKind

    private let store: any TokenStore
    private let session: URLSession
    private let now: @Sendable () -> Date

    private var cachedConfig: CloudConfig?
    private var cachedOIDC: (client: OIDCClient, provider: OIDCProvider)?
    /// The refresh in flight; parallel callers wait for it instead of starting their own.
    private var refreshing: (id: Int, task: Task<TokenSet, any Error>)?
    private var nextRefreshID = 0
    /// Bumped by `signIn` and `signOut`: a refresh that started before must not write over their result.
    private var generation = 0

    public init(
        cloud: URL,
        kind: AccountClientKind,
        store: any TokenStore,
        session: URLSession = .shared,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.cloud = cloud
        self.kind = kind
        self.store = store
        self.session = session
        self.now = now
        self.cloudClient = CloudClient(cloud: cloud, session: session)
    }

    /// Whether tokens are stored (an unreadable store counts as signed out).
    public var isSignedIn: Bool { ((try? store.load()) ?? nil) != nil }

    /// `GET /v1/config`, kept in memory after the first success.
    public func config() async throws -> CloudConfig {
        if let cachedConfig { return cachedConfig }
        let config = try await cloudClient.config()
        cachedConfig = config
        return config
    }

    /// The OIDC client of this app and its provider (issuer and client id from `/v1/config`), kept in memory
    /// after the first success.
    public func oidc() async throws -> (OIDCClient, OIDCProvider) {
        if let cachedOIDC { return (cachedOIDC.client, cachedOIDC.provider) }
        let config = try await config()
        let clientID = switch kind {
        case .ios: config.clients.ios
        case .watch: config.clients.watch
        }
        guard let clientID, !clientID.isEmpty, OIDCClient.isSecure(config.issuer) else { throw AccountError.notConfigured }
        let client = OIDCClient(issuer: config.issuer, clientID: clientID, session: session, now: now)
        let provider = try await client.discover()
        cachedOIDC = (client, provider)
        return (client, provider)
    }

    /// Stores the tokens of a new login. A refresh still in flight will not write over them.
    public func signIn(_ tokens: TokenSet) throws {
        generation += 1
        // New callers must not wait for (and get the result of) a refresh of the previous login.
        refreshing = nil
        try store.save(tokens)
    }

    /// An access token with at least 60 s to live. Refreshes when needed, one refresh at a time (parallel
    /// calls wait for the same one). A refused refresh token (`invalid_grant`) deletes the tokens and throws
    /// `.signedOut`; a network failure keeps them.
    public func accessToken() async throws -> String {
        if let refreshing {
            return try await refreshing.task.value.accessToken
        }
        guard let tokens = try store.load() else { throw AccountError.signedOut }
        if tokens.isFresh(at: now()) { return tokens.accessToken }
        guard let refreshToken = tokens.refreshToken else {
            // Nothing to renew it with: the session is over.
            try? store.delete()
            generation += 1
            throw AccountError.signedOut
        }
        nextRefreshID += 1
        let id = nextRefreshID
        let generation = generation
        let task = Task { try await self.refresh(refreshToken, generation: generation) }
        refreshing = (id, task)
        defer {
            if self.refreshing?.id == id { self.refreshing = nil }
        }
        return try await task.value.accessToken
    }

    /// A per-server token for `server`, only when the server's central account is this Cloud
    /// (`health.account.issuer` against the Cloud URL, canonical forms). The reply must name `server` as audience.
    public func serverToken(for server: URL, health: ServerHealth) async throws -> String {
        guard let account = health.account else { throw AccountError.serverWithoutAccount }
        guard let issuer = URL(string: account.issuer),
              ServerAddress.canonical(issuer) == ServerAddress.canonical(cloud)
        else { throw AccountError.foreignIssuer(account.issuer) }
        let token = try await cloudClient.serverToken(audience: server, accessToken: try await accessToken())
        guard URL(string: token.audience).map(ServerAddress.canonical) == ServerAddress.canonical(server) else {
            throw APIError.malformedResponse
        }
        return token.token
    }

    /// Deletes the tokens, then revokes the refresh token (the access token when there is none) at the
    /// provider. Revocation errors (offline, Cloud or issuer down) are ignored: the device is signed out anyway.
    /// Opening the end-session URL is the app's job (`OIDCClient.endSessionURL`).
    public func signOut() async {
        generation += 1
        refreshing?.task.cancel()
        refreshing = nil
        let tokens = (try? store.load()) ?? nil
        try? store.delete()
        guard let tokens else { return }
        do {
            let (client, provider) = try await oidc()
            try await client.revoke(tokens.refreshToken ?? tokens.accessToken, provider)
        } catch {
            // Ignored on purpose (see above); nothing is logged.
        }
    }

    // MARK: - Refresh

    private func refresh(_ refreshToken: String, generation: Int) async throws -> TokenSet {
        let (client, provider) = try await oidc()
        do {
            let tokens = try await client.refresh(refreshToken, provider)
            if generation == self.generation {
                try store.save(tokens)
            }
            return tokens
        } catch OIDCError.invalidGrant {
            if generation == self.generation {
                try? store.delete()
                self.generation += 1
            }
            throw AccountError.signedOut
        }
    }
}
