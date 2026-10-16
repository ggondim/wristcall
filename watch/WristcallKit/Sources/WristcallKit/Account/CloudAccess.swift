import Foundation

/// What the rest of the app needs from "the account on this device", whichever session backs it: the Zitadel
/// login (`AccountSession`) or the Cloud device session (`DeviceSession`).
public protocol CloudAccess: Sendable {
    var cloudClient: CloudClient { get }
    var isSignedIn: Bool { get async }
    /// An access token the Cloud accepts, renewed first when it is about to expire.
    func accessToken() async throws -> String
    /// A per-server token for `server`, only when the server's central account is this Cloud.
    func serverToken(for server: URL, health: ServerHealth) async throws -> String
}

extension AccountSession: CloudAccess {}

extension CloudAccess {
    /// A per-server token for `server`, only when the server's central account is this Cloud
    /// (`health.account.issuer` against the Cloud URL, canonical forms). The reply must name `server` as audience.
    public func serverToken(for server: URL, health: ServerHealth) async throws -> String {
        guard let account = health.account else { throw AccountError.serverWithoutAccount }
        guard let issuer = URL(string: account.issuer),
              ServerAddress.canonical(issuer) == ServerAddress.canonical(cloudClient.cloud)
        else { throw AccountError.foreignIssuer(account.issuer) }
        let token = try await cloudClient.serverToken(audience: server, accessToken: try await accessToken())
        guard URL(string: token.audience).map(ServerAddress.canonical) == ServerAddress.canonical(server) else {
            throw APIError.malformedResponse
        }
        return token.token
    }
}
