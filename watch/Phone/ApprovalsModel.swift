import Foundation
import Observation
import WristcallKit

/// What a notification action does with a device approval request.
enum ApprovalAction: Equatable, Sendable {
    case approve
    case deny
}

/// The login requests from watches waiting for the owner's approval, across all servers. Approving hands
/// a device credential to whoever started the request, so every path here checks what it sends.
@MainActor
@Observable
final class ApprovalsModel {
    struct Pending: Identifiable, Equatable {
        let serverID: String
        let serverName: String
        let request: ApprovalRequest
        var id: String { serverID + "/" + request.requestId }
    }

    static let expiredNotice = "This request expired."

    /// Waiting requests, in server order, then by expiry.
    private(set) var pending: [Pending] = []
    /// What the last approve/deny could not do, for the screen (or a local notice) to show. `nil` clears it.
    var notice: String?
    /// Ids of the requests being sent right now (the buttons turn off).
    private(set) var busy: Set<String> = []

    /// A notification asked to show the devices of a server: the Servers tab, then that server's devices
    /// (`serverID` is the push's tag, looked up among the saved servers only). Each tap is a new value.
    struct OpenRequest: Equatable {
        let id = UUID()
        let serverID: String?
    }

    /// Set by a tapped notification; the screen that shows it sets it back to `nil`.
    var openRequest: OpenRequest?

    func open(serverID: String?) {
        openRequest = OpenRequest(serverID: serverID)
    }

    @ObservationIgnored private let state: AppState
    @ObservationIgnored private let now: () -> Date
    @ObservationIgnored private var refreshesRunning = 0
    /// Requests answered while a refresh was in flight: that refresh read the list before the answer and
    /// must not bring them back. Cleared when no refresh is running (the 4 digit ids repeat over time).
    @ObservationIgnored private var answeredDuringRefresh: Set<String> = []

    init(state: AppState, now: @escaping () -> Date = Date.init) {
        self.state = state
        self.now = now
    }

    /// A request id is exactly four ASCII digits (`^[0-9]{4}$`). Anything else never leaves the app: the id
    /// goes into a URL path.
    nonisolated static func isValidRequestID(_ id: String) -> Bool {
        id.utf8.count == 4 && id.utf8.allSatisfy { (0x30...0x39).contains($0) }
    }

    // MARK: Listing

    /// Asks every server with a central account, in parallel, for its waiting requests. A server that
    /// answers `not_configured` or rejects the token has none; a failure to connect keeps what was shown.
    func refresh() async {
        refreshesRunning += 1
        defer {
            refreshesRunning -= 1
            if refreshesRunning == 0 { answeredDuringRefresh = [] }
        }

        // Only servers that said they have an account: an old server answers 404 to the route, not
        // `not_configured`, and a server whose health is unknown has nothing to ask yet.
        let targets = state.servers.filter { state.healths[$0.id]?.account != nil }
        let asked = targets.map { ($0, state.api(for: $0)) }
        let results = await withTaskGroup(of: (String, Result<[ApprovalRequest], any Error>).self) { group in
            for (server, api) in asked {
                let id = server.id
                group.addTask {
                    do { return (id, .success(try await api.pairingRequests())) } catch { return (id, .failure(error)) }
                }
            }
            var all: [String: Result<[ApprovalRequest], any Error>] = [:]
            for await (id, result) in group { all[id] = result }
            return all
        }

        let current = now().timeIntervalSince1970
        var merged: [Pending] = []
        for server in state.servers {
            guard let result = results[server.id] else { continue }
            switch result {
            case .success(let requests):
                let valid = requests
                    .filter { Self.isValidRequestID($0.requestId) && $0.expiresAt > current }
                    .map { Pending(serverID: server.id, serverName: server.name, request: $0) }
                    .filter { !answeredDuringRefresh.contains($0.id) }
                    .sorted { $0.request.expiresAt < $1.request.expiresAt }
                merged += valid
            case .failure(let error):
                if Self.hasNone(error) { continue }
                // A blip: keep what that server had.
                merged += pending.filter { $0.serverID == server.id && $0.request.expiresAt > current }
                    .filter { !answeredDuringRefresh.contains($0.id) }
            }
        }
        if merged != pending { pending = merged }
    }

    /// The server has no requests for this token: no account, or the token is not accepted.
    private static func hasNone(_ error: any Error) -> Bool {
        switch error as? APIError {
        case .notConfigured, .notFound, .unauthorized, .forbidden, .notLinked: true
        default: false
        }
    }

    // MARK: Answering

    func approve(_ item: Pending) async {
        _ = await answer(.approve, serverID: item.serverID, requestID: item.request.requestId)
    }

    func deny(_ item: Pending) async {
        _ = await answer(.deny, serverID: item.serverID, requestID: item.request.requestId)
    }

    /// A notification action (Task 9): finds the server by the local id the push carried (its `tag`),
    /// checks the request id, and answers. Nothing from the push but the id is used. Returns `true` only
    /// when the server accepted the answer.
    ///
    /// The expiry must be known before anything is sent: the 4 digit ids are reused, so a stale action could
    /// otherwise hit a newer request. It is the push's `expiresAt`, or else the one of the request with
    /// this id as the server lists it right now. With neither, or when the request is expired or no longer
    /// listed, nothing is answered (`false`; the caller opens the app) and no request is approved blind.
    func handle(action: ApprovalAction, serverID: String, requestID: String, expiresAt: Date? = nil) async -> Bool {
        guard Self.isValidRequestID(requestID) else { return false }
        // Started by the action: the list may not have been read, and the health is not known.
        guard state.loadStoreIfNeeded(), let server = state.servers.first(where: { $0.id == serverID }) else { return false }
        var expiry = expiresAt
        if expiry == nil {
            do {
                let listed = try await state.api(for: server).pairingRequests()
                expiry = listed.first { $0.requestId == requestID }.map { Date(timeIntervalSince1970: $0.expiresAt) }
            } catch {
                notice = APIError.text(error)
                return false
            }
        }
        guard let expiry, expiry > now() else {
            drop(serverID: serverID, requestID: requestID)
            notice = Self.expiredNotice
            return false
        }
        return await answer(action, serverID: serverID, requestID: requestID)
    }

    private func answer(_ action: ApprovalAction, serverID: String, requestID: String) async -> Bool {
        guard Self.isValidRequestID(requestID) else { return false }
        guard state.loadStoreIfNeeded(), let server = state.servers.first(where: { $0.id == serverID }) else {
            drop(serverID: serverID, requestID: requestID)
            return false
        }
        let key = serverID + "/" + requestID
        guard busy.insert(key).inserted else { return false }
        defer { busy.remove(key) }

        let api = state.api(for: server)
        do {
            switch action {
            case .approve: _ = try await api.approve(requestID: requestID)
            case .deny: try await api.deny(requestID: requestID)
            }
        } catch APIError.notFound {
            drop(serverID: serverID, requestID: requestID)
            notice = Self.expiredNotice
            return false
        } catch {
            notice = APIError.text(error)
            return false
        }
        drop(serverID: serverID, requestID: requestID)
        notice = nil
        return true
    }

    private func drop(serverID: String, requestID: String) {
        if refreshesRunning > 0 { answeredDuringRefresh.insert(serverID + "/" + requestID) }
        pending.removeAll { $0.serverID == serverID && $0.request.requestId == requestID }
    }
}
