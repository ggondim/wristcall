import Foundation

/// What the watch does after the account login (decision R11), without UI: for one server of the agenda,
/// `GET /v1/health` (the server's account must be this Cloud) → `POST {cloud}/v1/server-tokens` →
/// `POST /v1/pair/account`. Nothing here logs; tokens never reach an outcome or a reason.
public struct AccountPairing: Sendable {
    public enum Outcome: Sendable, Equatable {
        /// The server paired the watch at once (`device_credential: direct`).
        case paired(URL, PairedDevice)
        /// The account's owner approves it on the iPhone; poll with the request (as in flow B).
        case pending(URL, PairingRequest)
        /// Not paired, with a short reason for the list (`Reason`).
        case skipped(URL, String)
    }

    /// Short reasons of `.skipped`, shown after the server's host ("y.test: not linked to your account").
    public enum Reason {
        public static let oldServer = "server needs 0.6.0"
        public static let noAccount = "no account on this server"
        public static let foreignAccount = "uses another account service"
        public static let notLinked = "not linked to your account"
        public static let limit = "device limit reached"
        public static let rateLimited = "too many attempts, try later"
        public static let tooManyRequests = "too many requests waiting for approval"
        public static let accountUnavailable = "account service unavailable"
        public static let unreachable = "can't reach it"
        public static let signedOut = "sign in again"
        public static let noToken = "wristcall Cloud gave no token"
        public static let unexpected = "unexpected reply"
    }

    /// The first server version with per-server tokens (`POST /v1/pair/account` takes only those).
    public static let minimumVersion = [0, 6, 0]

    private let session: AccountSession
    private let pairing: PairingClient
    private let http: URLSession

    /// `http` reads each server's health (`ServerPushClient.health(of:session:)`).
    public init(session: AccountSession, pairing: PairingClient = PairingClient(), http: URLSession = .shared) {
        self.session = session
        self.pairing = pairing
        self.http = http
    }

    /// The agenda's servers this watch is not paired with (compared by `ServerAddress.canonical`), in the
    /// agenda's order, each address once. Only addresses `ServerAddress.parse` accepts (`https://`, or `http://`
    /// on loopback): the Cloud stores any `http://`. `linked` is not a filter (I6a): the server answers
    /// `not_linked` itself, and the agenda may lag behind a link made elsewhere.
    public static func candidates(_ agenda: [CloudServer], paired: [URL]) -> [URL] {
        var seen = Set(paired.map(ServerAddress.canonical))
        return agenda.compactMap { server in
            guard let url = ServerAddress.parse(server.url), seen.insert(ServerAddress.canonical(url)).inserted else {
                return nil
            }
            return url
        }
    }

    /// Whether `version` (`0.6.0`, `0.6.1rc1`, `1.0`…) is at least `minimumVersion`. Anything unreadable is not.
    public static func supportsAccountPairing(version: String) -> Bool {
        let parts = version.split(separator: ".", omittingEmptySubsequences: false).map { part in
            Int(part.prefix(while: { $0.isASCII && $0.isNumber }))
        }
        guard let first = parts.first, first != nil else { return false }
        let numbers = parts.prefix(while: { $0 != nil }).map { $0! }
        for (index, minimum) in minimumVersion.enumerated() {
            let value = index < numbers.count ? numbers[index] : 0
            if value != minimum { return value > minimum }
        }
        return true
    }

    /// Pairs the watch with `server` for the signed-in account. The server is skipped, before any token is asked
    /// for (M10), when its health cannot be read, it is older than 0.6.0, it has no central account, or its
    /// account is another Cloud (Review Focus 2).
    public func pair(_ server: URL, deviceName: String) async -> Outcome {
        let health: ServerHealth
        do {
            health = try await ServerPushClient.health(of: server, session: http)
        } catch PairingError.network {
            return .skipped(server, Reason.unreachable)
        } catch {
            return .skipped(server, Reason.unexpected)
        }
        guard Self.supportsAccountPairing(version: health.version) else { return .skipped(server, Reason.oldServer) }
        guard let account = health.account else { return .skipped(server, Reason.noAccount) }
        guard let issuer = URL(string: account.issuer),
              ServerAddress.canonical(issuer) == ServerAddress.canonical(session.cloud)
        else { return .skipped(server, Reason.foreignAccount) }

        let token: String
        do {
            token = try await session.serverToken(for: server, health: health)
        } catch AccountError.signedOut {
            return .skipped(server, Reason.signedOut)
        } catch AccountError.foreignIssuer {
            return .skipped(server, Reason.foreignAccount)
        } catch AccountError.serverWithoutAccount {
            return .skipped(server, Reason.noAccount)
        } catch {
            return .skipped(server, Reason.noToken)
        }

        do {
            switch try await pairing.pairWithAccount(server: server, serverToken: token, deviceName: deviceName) {
            case .paired(let device): return .paired(server, device)
            case .pending(let request): return .pending(server, request)
            }
        } catch let error as PairingError {
            return .skipped(server, Self.reason(for: error))
        } catch {
            return .skipped(server, Reason.unexpected)
        }
    }

    static func reason(for error: PairingError) -> String {
        switch error {
        // A server before 0.6.0 refuses the per-server token: the version check above missed it.
        case .accountRejected: Reason.oldServer
        case .notLinked: Reason.notLinked
        case .limit: Reason.limit
        case .accountNotConfigured: Reason.noAccount
        case .rateLimited: Reason.rateLimited
        case .tooManyRequests: Reason.tooManyRequests
        case .accountUnavailable: Reason.accountUnavailable
        case .network: Reason.unreachable
        default: Reason.unexpected
        }
    }
}
