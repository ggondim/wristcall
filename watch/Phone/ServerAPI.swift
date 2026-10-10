import Foundation
import WristcallKit

/// What the screens use of one server. `LiveServerAPI` talks to the server; the tests use a fake.
protocol ServerAPI: Sendable {
    /// `GET /v1/health`; needs no token.
    func health() async throws -> ServerHealth
    /// Succeeds for a personal token; `APIError.forbidden` for a device token.
    func verify() async throws

    func agents() async throws -> [AgentDetail]
    func agent(_ ref: String) async throws -> AgentDetail
    func createAgent(_ fields: AgentFields) async throws -> AgentDetail
    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail
    func deleteAgent(_ ref: String) async throws
    func providers() async throws -> ProviderList

    func devices() async throws -> [DeviceRecord]
    func revokeDevice(_ id: String) async throws
    func createPairingCode() async throws -> PairingCodeGrant
    func pairingRequests() async throws -> [ApprovalRequest]
    func approve(requestID: String) async throws -> String
    func deny(requestID: String) async throws

    func calls(_ query: HistoryQuery) async throws -> CallPage
    func deleteCall(_ id: String) async throws
    func deleteCalls(agent: String?) async throws -> Int
    func redeliver(_ id: String) async throws -> CallRecord
    func export(_ format: ExportFormat, agent: String?) async throws -> HistoryExport

    /// Links the signed-in account to the user of this token (`serverToken` is the Cloud's token for it).
    func linkAccount(accountToken: String) async throws -> AccountLink
}

/// The real thing: the Kit's clients with the server's personal token.
struct LiveServerAPI: ServerAPI {
    private let server: URL
    private let management: ManagementClient
    private let history: HistoryClient

    init(server: URL, token: String, session: URLSession = .shared) {
        self.server = server
        management = ManagementClient(server: server, token: token, session: session)
        history = HistoryClient(server: server, token: token, session: session)
    }

    func health() async throws -> ServerHealth {
        try await ServerPushClient.health(of: server)
    }
    func verify() async throws { try await management.verify() }

    func agents() async throws -> [AgentDetail] { try await management.agents() }
    func agent(_ ref: String) async throws -> AgentDetail { try await management.agent(ref) }
    func createAgent(_ fields: AgentFields) async throws -> AgentDetail { try await management.createAgent(fields) }
    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail {
        try await management.updateAgent(ref, fields)
    }
    func deleteAgent(_ ref: String) async throws { try await management.deleteAgent(ref) }
    func providers() async throws -> ProviderList { try await management.providers() }

    func devices() async throws -> [DeviceRecord] { try await management.devices() }
    func revokeDevice(_ id: String) async throws { try await management.revokeDevice(id) }
    func createPairingCode() async throws -> PairingCodeGrant { try await management.createPairingCode() }
    func pairingRequests() async throws -> [ApprovalRequest] { try await management.pairingRequests() }
    func approve(requestID: String) async throws -> String { try await management.approve(requestID: requestID) }
    func deny(requestID: String) async throws { try await management.deny(requestID: requestID) }

    func calls(_ query: HistoryQuery) async throws -> CallPage { try await history.calls(query) }
    func deleteCall(_ id: String) async throws { try await history.deleteCall(id) }
    func deleteCalls(agent: String?) async throws -> Int { try await history.deleteCalls(agent: agent) }
    func redeliver(_ id: String) async throws -> CallRecord { try await history.redeliver(id) }
    func export(_ format: ExportFormat, agent: String?) async throws -> HistoryExport {
        try await history.export(format, agent: agent)
    }

    func linkAccount(accountToken: String) async throws -> AccountLink {
        try await management.linkAccount(serverToken: accountToken)
    }
}
