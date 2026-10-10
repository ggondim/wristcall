import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct AppStateTests {
    let store = InMemoryManagedServerStore()

    private func state(_ store: InMemoryManagedServerStore? = nil, fake: FakeServerAPI = FakeServerAPI()) -> AppState {
        AppState(store: store ?? self.store, makeAPI: { _, _ in fake })
    }

    @Test func addServerRejectsDeviceToken() async throws {
        let fake = FakeServerAPI()
        fake.verifyError = APIError.forbidden("this needs a user API token, not a device token")
        let state = AppState(store: InMemoryManagedServerStore(), makeAPI: { _, _ in fake })
        await #expect(throws: AddServerError.notPersonalToken) {
            try await state.addServer(urlText: "https://srv.test", token: "abcdef", name: nil)
        }
        await #expect(throws: AddServerError.deviceToken) {
            try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        }
        #expect(state.servers.isEmpty)
    }

    @Test func addServerTrimsToken() async throws {
        let store = InMemoryManagedServerStore()
        let state = AppState(store: store, makeAPI: { _, token in
            #expect(token == "wc_pat_abc")
            return FakeServerAPI()
        })
        _ = try await state.addServer(urlText: " srv.test/ ", token: " wc_pat_abc\n", name: nil)
        #expect(try store.load().first?.url.absoluteString == "https://srv.test")
        #expect(try store.load().first?.name == "srv.test")
        #expect(try store.load().first?.token == "wc_pat_abc")
    }

    @Test func addServerVerifiesBeforeSaving() async throws {
        let fake = FakeServerAPI()
        fake.verifyError = APIError.unauthorized
        let state = state(fake: fake)
        await #expect(throws: AddServerError.unauthorized) {
            try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        }
        #expect(try store.load().isEmpty)
        #expect(state.servers.isEmpty)
    }

    @Test func addServerReportsUnreachable() async throws {
        let fake = FakeServerAPI()
        fake.verifyError = APIError.network(.cannotConnectToHost)
        let state = state(fake: fake)
        await #expect(throws: AddServerError.unreachable(APIError.network(.cannotConnectToHost).message)) {
            try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        }
        #expect(state.servers.isEmpty)
    }

    @Test func addServerKeepsAGivenName() async throws {
        let state = state()
        let server = try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: "  Home ")
        #expect(server.name == "Home")
        #expect(state.servers == [server])
        #expect(try store.load() == [server])
        #expect(state.statuses[server.id] == .reachable)
    }

    @Test func sameURLReplacesToken() async throws {
        let state = state()
        let first = try await state.addServer(urlText: "https://srv.test", token: "wc_pat_old", name: "Home")
        let second = try await state.addServer(urlText: "HTTPS://Srv.test:443/", token: "wc_pat_new", name: nil)
        #expect(second.id == first.id)
        #expect(second.name == "Home")
        #expect(state.servers.count == 1)
        #expect(try store.load().first?.token == "wc_pat_new")
    }

    @Test func invalidURLRejected() async throws {
        let state = state()
        await #expect(throws: AddServerError.invalidURL) {
            try await state.addServer(urlText: "http://example.com", token: "wc_pat_x", name: nil)
        }
        await #expect(throws: AddServerError.invalidURL) {
            try await state.addServer(urlText: "  ", token: "wc_pat_x", name: nil)
        }
        #expect(state.servers.isEmpty)
    }

    @Test func loadMarksReachableServers() async throws {
        let server = ManagedServer(id: "a", name: "A", url: URL(string: "https://a.test")!, token: "wc_pat_a")
        let fake = FakeServerAPI()
        fake.healthResult = .success(ServerHealth(version: "0.9.1"))
        let state = state(InMemoryManagedServerStore([server]), fake: fake)
        await state.load()
        #expect(state.servers == [server])
        #expect(state.statuses["a"] == .reachable)
        #expect(state.healths["a"]?.version == "0.9.1")
    }

    @Test func loadMarksUnauthorized() async throws {
        let server = ManagedServer(id: "a", name: "A", url: URL(string: "https://a.test")!, token: "wc_pat_a")
        let fake = FakeServerAPI()
        fake.verifyError = APIError.unauthorized
        let state = state(InMemoryManagedServerStore([server]), fake: fake)
        await state.load()
        #expect(state.statuses["a"] == .unauthorized)
    }

    @Test func loadMarksUnreachable() async throws {
        let server = ManagedServer(id: "a", name: "A", url: URL(string: "https://a.test")!, token: "wc_pat_a")
        let fake = FakeServerAPI()
        fake.healthError = APIError.network(.timedOut)
        let state = state(InMemoryManagedServerStore([server]), fake: fake)
        await state.load()
        #expect(state.statuses["a"] == .unreachable)
        #expect(state.healths["a"] == nil)
    }

    @Test func loadChecksEveryServer() async throws {
        let servers = (1...3).map {
            ManagedServer(id: "s\($0)", name: "S\($0)", url: URL(string: "https://s\($0).test")!, token: "wc_pat_\($0)")
        }
        let state = AppState(store: InMemoryManagedServerStore(servers), makeAPI: { url, _ in
            let fake = FakeServerAPI()
            if url.host == "s2.test" { fake.healthError = APIError.network(.cannotFindHost) }
            return fake
        })
        await state.load()
        #expect(state.statuses == ["s1": .reachable, "s2": .unreachable, "s3": .reachable])
    }

    @Test func renameSavesTrimmedName() async throws {
        let state = state()
        let server = try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        try state.rename(server.id, to: "  Office  ")
        #expect(state.servers.first?.name == "Office")
        #expect(try store.load().first?.name == "Office")
        #expect(throws: ServerNameError.empty) { try state.rename(server.id, to: "   ") }
        #expect(state.servers.first?.name == "Office")
        try state.rename(server.id, to: String(repeating: "x", count: 100))
        #expect(state.servers.first?.name.count == 64)
    }

    @Test func removeCallsHook() async throws {
        let state = state()
        let server = try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        var removed: [String] = []
        var added: [String] = []
        state.hooks.serverRemoved = { removed.append($0.id) }
        state.hooks.serverAdded = { added.append($0.id) }
        await state.remove(server.id)
        #expect(removed == [server.id])
        #expect(state.servers.isEmpty)
        #expect(state.statuses[server.id] == nil)
        #expect(try store.load().isEmpty)
        let again = try await state.addServer(urlText: "https://srv.test", token: "wc_pat_x", name: nil)
        #expect(added == [again.id])
    }

    @Test func removingAnUnknownServerDoesNothing() async {
        let state = state()
        var removed = 0
        state.hooks.serverRemoved = { _ in removed += 1 }
        await state.remove("nope")
        #expect(removed == 0)
    }

    @Test func storeFailureOnLoadIsReported() async {
        struct Broken: ManagedServerStore {
            func load() throws -> [ManagedServer] { throw CredentialStoreError.corruptedData }
            func save(_ servers: [ManagedServer]) throws {}
        }
        let state = AppState(store: Broken(), makeAPI: { _, _ in FakeServerAPI() })
        await state.load()
        #expect(state.servers.isEmpty)
        #expect(state.loadError != nil)
    }
}
