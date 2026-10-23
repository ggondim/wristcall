import Foundation
import WristcallKit

/// The Wristcall Cloud entry's `ServerAPI`: every request asks the router for the `manage` route and goes to the
/// node it names, with that node's token (decision D4: the route is cached until 30 s before it expires). A `401`
/// from the node drops the route and the request is tried once more with a new one. Routes a node does not have
/// (devices, pairing, approvals, push keys, account links) are `APIError.notFound` without any request.
struct RoutedServerAPI: ServerAPI {
    private let router: any CloudRouting
    private let make: ServerAPIFactory

    init(router: any CloudRouting, make: @escaping ServerAPIFactory = { LiveServerAPI(server: $0, token: $1) }) {
        self.router = router
        self.make = make
    }

    private func node() async throws -> any ServerAPI {
        let route = try await router.manage()
        return make(route.primary.url, route.primary.token)
    }

    private func routed<T: Sendable>(_ work: (any ServerAPI) async throws -> T) async throws -> T {
        // The route's own errors (a `401` from the Cloud included) pass through unchanged: only the node's `401`
        // means the route token is stale.
        let first = try await node()
        do {
            return try await work(first)
        } catch APIError.unauthorized {
            await router.invalidate()
            let second = try await node()
            do {
                return try await work(second)
            } catch APIError.unauthorized {
                // Refused again: the next request starts from a new route too.
                await router.invalidate()
                throw APIError.unauthorized
            }
        }
    }

    func health() async throws -> ServerHealth { try await routed { try await $0.health() } }
    func verify() async throws { try await routed { try await $0.verify() } }
    func me() async throws -> NodeAccount { try await routed { try await $0.me() } }

    func agents() async throws -> [AgentDetail] { try await routed { try await $0.agents() } }
    func agent(_ ref: String) async throws -> AgentDetail { try await routed { try await $0.agent(ref) } }
    func createAgent(_ fields: AgentFields) async throws -> AgentDetail {
        try await routed { try await $0.createAgent(fields) }
    }
    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail {
        try await routed { try await $0.updateAgent(ref, fields) }
    }
    func deleteAgent(_ ref: String) async throws { try await routed { try await $0.deleteAgent(ref) } }
    func providers() async throws -> ProviderList { try await routed { try await $0.providers() } }

    func calls(_ query: HistoryQuery) async throws -> CallPage { try await routed { try await $0.calls(query) } }
    func deleteCall(_ id: String) async throws { try await routed { try await $0.deleteCall(id) } }
    func deleteCalls(agent: String?) async throws -> Int { try await routed { try await $0.deleteCalls(agent: agent) } }
    func redeliver(_ id: String) async throws -> CallRecord { try await routed { try await $0.redeliver(id) } }
    func call(_ id: String) async throws -> CallRecord { try await routed { try await $0.call(id) } }
    func export(_ format: ExportFormat, agent: String?, since: Date?, until: Date?) async throws -> HistoryExport {
        try await routed { try await $0.export(format, agent: agent, since: since, until: until) }
    }

    // Not on a node: the Cloud keeps the account's devices, and push and links belong to self-hosted servers.
    func devices() async throws -> [DeviceRecord] { throw APIError.notFound }
    func revokeDevice(_ id: String) async throws { throw APIError.notFound }
    func createPairingCode() async throws -> PairingCodeGrant { throw APIError.notFound }
    func pairingRequests() async throws -> [ApprovalRequest] { throw APIError.notFound }
    func approve(requestID: String) async throws -> String { throw APIError.notFound }
    func deny(requestID: String) async throws { throw APIError.notFound }
    func setPushKey(_ key: String) async throws { throw APIError.notFound }
    func clearPushKey() async throws { throw APIError.notFound }
    func linkAccount(serverToken: String) async throws -> AccountLink { throw APIError.notFound }
    func unlinkAccount() async throws { throw APIError.notFound }
    func linkAccount(serverToken: String, code: PairingCode) async throws -> AccountLink { throw APIError.notFound }
    func linkAccount(serverToken: String, code: LinkCode) async throws -> AccountLink { throw APIError.notFound }
}

/// The Cloud entry's API before the account model set one (`AppState.cloudAPI`): no node to talk to.
struct UnroutedServerAPI: ServerAPI {
    private var none: APIError { .noNodeAvailable(retryAfter: nil) }

    func health() async throws -> ServerHealth { throw none }
    func verify() async throws { throw none }
    func me() async throws -> NodeAccount { throw none }
    func agents() async throws -> [AgentDetail] { throw none }
    func agent(_ ref: String) async throws -> AgentDetail { throw none }
    func createAgent(_ fields: AgentFields) async throws -> AgentDetail { throw none }
    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail { throw none }
    func deleteAgent(_ ref: String) async throws { throw none }
    func providers() async throws -> ProviderList { throw none }
    func devices() async throws -> [DeviceRecord] { throw none }
    func revokeDevice(_ id: String) async throws { throw none }
    func createPairingCode() async throws -> PairingCodeGrant { throw none }
    func pairingRequests() async throws -> [ApprovalRequest] { throw none }
    func approve(requestID: String) async throws -> String { throw none }
    func deny(requestID: String) async throws { throw none }
    func calls(_ query: HistoryQuery) async throws -> CallPage { throw none }
    func deleteCall(_ id: String) async throws { throw none }
    func deleteCalls(agent: String?) async throws -> Int { throw none }
    func redeliver(_ id: String) async throws -> CallRecord { throw none }
    func call(_ id: String) async throws -> CallRecord { throw none }
    func export(_ format: ExportFormat, agent: String?, since: Date?, until: Date?) async throws -> HistoryExport {
        throw none
    }
    func setPushKey(_ key: String) async throws { throw none }
    func clearPushKey() async throws { throw none }
    func linkAccount(serverToken: String) async throws -> AccountLink { throw none }
    func unlinkAccount() async throws { throw none }
    func linkAccount(serverToken: String, code: PairingCode) async throws -> AccountLink { throw none }
    func linkAccount(serverToken: String, code: LinkCode) async throws -> AccountLink { throw none }
}
