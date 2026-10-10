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
    /// The waits of the device flow's poll (tests pass one that returns at once).
    private let sleep: OIDCClient.Sleep

    private var cachedConfig: CloudConfig?
    private var cachedOIDC: (client: OIDCClient, provider: OIDCProvider)?
    /// The refresh in flight; parallel callers wait for it instead of starting their own.
    private var refreshing: (id: Int, task: Task<TokenSet, any Error>)?
    private var nextRefreshID = 0
    /// Bumped by `signIn` and `signOut` (and a refused refresh): a refresh that started before must neither write
    /// over their result nor hand its tokens to anyone.
    private var generation = 0
    /// Refreshed tokens the store could not save (a Keychain write failed). They win over the store, so the
    /// rotated refresh token is not lost; every read tries the save again.
    private var unsaved: TokenSet?

    public init(
        cloud: URL,
        kind: AccountClientKind,
        store: any TokenStore,
        session: URLSession = .shared,
        now: @escaping @Sendable () -> Date = { Date() },
        sleep: @escaping OIDCClient.Sleep = { try await Task.sleep(for: $0) }
    ) {
        self.cloud = cloud
        self.kind = kind
        self.store = store
        self.session = session
        self.now = now
        self.sleep = sleep
        self.cloudClient = CloudClient(cloud: cloud, session: session)
    }

    /// Whether there are tokens (an unreadable store counts as signed out).
    public var isSignedIn: Bool { unsaved != nil || ((try? store.load()) ?? nil) != nil }

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
        let client = OIDCClient(issuer: config.issuer, clientID: clientID, session: session, sleep: sleep, now: now)
        let provider = try await client.discover()
        cachedOIDC = (client, provider)
        return (client, provider)
    }

    /// Stores the tokens of a new login. A refresh still in flight will neither write over them nor hand its
    /// result to anyone: its waiters read these tokens instead.
    public func signIn(_ tokens: TokenSet) throws {
        generation += 1
        refreshing = nil
        unsaved = nil
        try store.save(tokens)
    }

    /// The watch's login (RFC 8628): a device code for this app's client with the Cloud's scopes. Show its
    /// `userCode`; keep the authorization (its device code is the poll's secret) for `completeDeviceAuthorization`.
    public func startDeviceAuthorization() async throws -> DeviceAuthorization {
        let config = try await config()
        let (client, provider) = try await oidc()
        return try await client.startDeviceAuthorization(provider, scopes: config.scopes)
    }

    /// Polls until the user approves `authorization` (`OIDCClient.pollDeviceToken`: `.expiredToken`,
    /// `.accessDenied`, cancellation), then stores the tokens as `signIn` does.
    public func completeDeviceAuthorization(_ authorization: DeviceAuthorization) async throws {
        let (client, provider) = try await oidc()
        let tokens = try await client.pollDeviceToken(authorization, provider)
        try Task.checkCancellation()
        try signIn(tokens)
    }

    /// An access token with at least 60 s to live. Refreshes when needed, one refresh at a time (parallel
    /// calls wait for the same one). A refused refresh token (`invalid_grant`) deletes the tokens and throws
    /// `.signedOut`; a network failure keeps them. A refresh outlived by `signIn` or `signOut` is discarded:
    /// its waiters start over with the new state (the new tokens, or `.signedOut`).
    public func accessToken() async throws -> String {
        while true {
            if let refreshing {
                switch await refreshing.task.result {
                case .success(let tokens): return tokens.accessToken
                case .failure(let error) where error is Superseded: continue
                case .failure(let error): throw error
                }
            }
            guard let tokens = try currentTokens() else { throw AccountError.signedOut }
            if tokens.isFresh(at: now()) { return tokens.accessToken }
            guard let refreshToken = tokens.refreshToken else {
                // Nothing to renew it with: the session is over.
                try? store.delete()
                unsaved = nil
                generation += 1
                throw AccountError.signedOut
            }
            nextRefreshID += 1
            let id = nextRefreshID
            let generation = generation
            let task = Task { try await self.refresh(refreshToken, idToken: tokens.idToken, generation: generation) }
            refreshing = (id, task)
            let result = await task.result
            if refreshing?.id == id { refreshing = nil }
            switch result {
            case .success(let tokens): return tokens.accessToken
            case .failure(let error) where error is Superseded: continue
            case .failure(let error): throw error
            }
        }
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
    /// With `postLogoutRedirect`, returns the end-session URL to open (with the login's ID token as
    /// `id_token_hint`; the URL is a secret too), or `nil` when the provider is unreachable or has none.
    @discardableResult
    public func signOut(postLogoutRedirect: URL? = nil) async -> URL? {
        generation += 1
        refreshing?.task.cancel()
        refreshing = nil
        let tokens = unsaved ?? ((try? store.load()) ?? nil)
        unsaved = nil
        try? store.delete()
        guard let (client, provider) = try? await oidc() else { return nil }
        if let tokens {
            // Ignored on purpose (see above); nothing is logged.
            try? await client.revoke(tokens.refreshToken ?? tokens.accessToken, provider)
        }
        guard let postLogoutRedirect else { return nil }
        return client.endSessionURL(provider, postLogoutRedirect: postLogoutRedirect, idTokenHint: tokens?.idToken)
    }

    // MARK: - Refresh

    /// The tokens in use: unsaved ones first (trying to save them again), then the store's.
    private func currentTokens() throws -> TokenSet? {
        guard let unsaved else { return try store.load() }
        if (try? store.save(unsaved)) != nil {
            self.unsaved = nil
        }
        return unsaved
    }

    private func refresh(_ refreshToken: String, idToken: String?, generation: Int) async throws -> TokenSet {
        do {
            let (client, provider) = try await oidc()
            var tokens = try await client.refresh(refreshToken, provider)
            guard generation == self.generation else { throw Superseded() }
            // The ID token only serves as the end-session hint: keep the login's one when none comes back.
            if tokens.idToken == nil { tokens.idToken = idToken }
            do {
                try store.save(tokens)
                unsaved = nil
            } catch {
                unsaved = tokens
            }
            return tokens
        } catch let error as Superseded {
            throw error
        } catch {
            guard generation == self.generation else { throw Superseded() }
            if case OIDCError.invalidGrant? = error as? OIDCError {
                try? store.delete()
                unsaved = nil
                self.generation += 1
                throw AccountError.signedOut
            }
            throw error
        }
    }
}

/// A refresh outlived by `signIn` or `signOut`: its result is nobody's.
private struct Superseded: Error {}
