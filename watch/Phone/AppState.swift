import Foundation
import Observation
import WristcallKit

/// Why a server could not be added. `message` is the text the form shows.
enum AddServerError: Error, Equatable {
    case invalidURL
    /// The text does not start with `wc_pat_`.
    case notPersonalToken
    /// The server took the token for a device token (it refused the management route).
    case deviceToken
    case unauthorized
    case unreachable(String)

    var message: String {
        switch self {
        case .invalidURL:
            "Enter an https:// address. Plain http:// only works for localhost."
        case .notPersonalToken:
            "That is not a personal token. Create one with `wristcall users tokens add --name iphone`."
        case .deviceToken:
            ManagementClient.deviceTokenMessage
        case .unauthorized:
            "The server did not accept this token. It may have been revoked."
        case .unreachable(let text):
            text.isEmpty ? "Can't reach the server." : text
        }
    }
}

enum ServerNameError: Error, Equatable {
    case empty
}

/// What the app knows of a server right now.
enum ServerStatus: Equatable, Sendable {
    case checking
    case reachable
    /// The server answers but refuses the token (revoked, or not a personal token).
    case unauthorized
    case unreachable
}

/// Hooks other features hang on (agenda sync, push registration); empty by default.
struct AppHooks {
    var serverAdded: (@MainActor (ManagedServer) async -> Void)?
    var serverRemoved: (@MainActor (ManagedServer) async -> Void)?
    var agentsChanged: (@MainActor (ManagedServer, [AgentDetail]) async -> Void)?
}

/// The list of servers the iPhone app manages, and what each one is doing.
@MainActor
@Observable
final class AppState {
    private(set) var servers: [ManagedServer] = []
    private(set) var statuses: [String: ServerStatus] = [:]
    private(set) var healths: [String: ServerHealth] = [:]
    /// Set when the Keychain list could not be read; the Servers screen shows it.
    private(set) var loadError: String?
    var hooks = AppHooks()

    @ObservationIgnored private let store: any ManagedServerStore
    @ObservationIgnored private let makeAPI: @Sendable (URL, String) -> any ServerAPI

    init(
        store: any ManagedServerStore = KeychainManagedServerStore(),
        makeAPI: @escaping @Sendable (URL, String) -> any ServerAPI = { LiveServerAPI(server: $0, token: $1) }
    ) {
        self.store = store
        self.makeAPI = makeAPI
    }

    func api(for server: ManagedServer) -> any ServerAPI { makeAPI(server.url, server.token) }

    /// Reads the Keychain, then asks every server for its health, in parallel.
    func load() async {
        do {
            servers = try store.load()
            loadError = nil
        } catch {
            servers = []
            loadError = "The saved servers could not be read from the Keychain."
        }
        statuses = Dictionary(uniqueKeysWithValues: servers.map { ($0.id, ServerStatus.checking) })
        healths = [:]
        await refresh()
    }

    /// Checks every server again (pull to refresh).
    func refresh() async {
        let checks = servers.map { ($0.id, api(for: $0)) }
        let results = await withTaskGroup(of: (String, ServerStatus, ServerHealth?).self) { group in
            for (id, api) in checks {
                group.addTask { await Self.check(id: id, api: api) }
            }
            var all: [(String, ServerStatus, ServerHealth?)] = []
            for await result in group { all.append(result) }
            return all
        }
        for (id, status, health) in results where statuses[id] != nil {
            statuses[id] = status
            healths[id] = health
        }
    }

    private nonisolated static func check(id: String, api: any ServerAPI) async -> (String, ServerStatus, ServerHealth?) {
        let health: ServerHealth
        do { health = try await api.health() } catch { return (id, .unreachable, nil) }
        do {
            try await api.verify()
            return (id, .reachable, health)
        } catch let error as APIError {
            switch error {
            case .unauthorized, .forbidden: return (id, .unauthorized, health)
            default: return (id, .unreachable, health)
            }
        } catch {
            return (id, .unreachable, health)
        }
    }

    /// `urlText` goes through `ServerAddress`; the token loses its surrounding spaces, must start with
    /// `wc_pat_` and is verified against the server before anything is saved. The same server added again
    /// (same canonical URL) keeps its id and name and gets the new token.
    @discardableResult
    func addServer(urlText: String, token: String, name: String?) async throws -> ManagedServer {
        guard let url = ServerAddress.parse(urlText) else { throw AddServerError.invalidURL }
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ManagementClient.isPersonalToken(token) else { throw AddServerError.notPersonalToken }

        let api = makeAPI(url, token)
        do {
            try await api.verify()
        } catch let error as APIError {
            switch error {
            case .unauthorized: throw AddServerError.unauthorized
            case .forbidden: throw AddServerError.deviceToken
            default: throw AddServerError.unreachable(error.message)
            }
        } catch {
            throw AddServerError.unreachable("Can't reach the server.")
        }

        let given = Self.cleanName(name)
        let canonical = ServerAddress.canonical(url)
        var list = servers
        let saved: ManagedServer
        if let index = list.firstIndex(where: { ServerAddress.canonical($0.url) == canonical }) {
            list[index].token = token
            if let given { list[index].name = given }
            saved = list[index]
        } else {
            saved = ManagedServer(name: given ?? url.host() ?? url.absoluteString, url: url, token: token)
            list.append(saved)
        }
        do {
            try store.save(list)
        } catch {
            throw AddServerError.unreachable("The server could not be saved to the Keychain.")
        }
        servers = list
        statuses[saved.id] = .reachable
        healths[saved.id] = try? await api.health()
        await hooks.serverAdded?(saved)
        return saved
    }

    func rename(_ id: String, to name: String) throws {
        guard let name = Self.cleanName(name) else { throw ServerNameError.empty }
        guard let index = servers.firstIndex(where: { $0.id == id }) else { return }
        var list = servers
        list[index].name = name
        try store.save(list)
        servers = list
    }

    /// Takes the server out of the Keychain. Its token keeps working on the server until revoked there.
    func remove(_ id: String) async {
        guard let server = servers.first(where: { $0.id == id }) else { return }
        let list = servers.filter { $0.id != id }
        do {
            try store.save(list)
        } catch {
            loadError = "The server could not be removed from the Keychain."
            return
        }
        servers = list
        statuses[id] = nil
        healths[id] = nil
        await hooks.serverRemoved?(server)
    }

    private static func cleanName(_ name: String?) -> String? {
        guard let trimmed = name?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else { return nil }
        return String(trimmed.prefix(64))
    }
}
