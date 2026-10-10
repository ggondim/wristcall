import Foundation
import Synchronization
import WristcallKit
@testable import WristcallPhone

/// A `ServerAPI` with programmable replies and a record of the calls it got. Errors and values are set
/// before the call under test; the class is only touched from the main actor in the tests.
final class FakeServerAPI: ServerAPI, @unchecked Sendable {
    var healthResult: Result<ServerHealth, any Error> = .success(ServerHealth(version: "1.0.0"))
    var verifyError: (any Error)?
    private(set) var calls: [String] = []

    var healthError: (any Error)? {
        get { if case .failure(let error) = healthResult { error } else { nil } }
        set { healthResult = newValue.map { .failure($0) } ?? .success(ServerHealth(version: "1.0.0")) }
    }

    func health() async throws -> ServerHealth {
        calls.append("health")
        return try healthResult.get()
    }

    func verify() async throws {
        calls.append("verify")
        if let verifyError { throw verifyError }
    }

    func agents() async throws -> [AgentDetail] { calls.append("agents"); return [] }
    func agent(_ ref: String) async throws -> AgentDetail { throw APIError.notFound }
    func createAgent(_ fields: AgentFields) async throws -> AgentDetail { throw APIError.notFound }
    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail { throw APIError.notFound }
    func deleteAgent(_ ref: String) async throws {}
    func providers() async throws -> ProviderList { ProviderList(providers: [], customEndpoints: false) }
    func devices() async throws -> [DeviceRecord] { [] }
    func revokeDevice(_ id: String) async throws {}
    func createPairingCode() async throws -> PairingCodeGrant { throw APIError.notFound }
    func pairingRequests() async throws -> [ApprovalRequest] { [] }
    func approve(requestID: String) async throws -> String { "" }
    func deny(requestID: String) async throws {}
    func calls(_ query: HistoryQuery) async throws -> CallPage { CallPage(calls: []) }
    func deleteCall(_ id: String) async throws {}
    func deleteCalls(agent: String?) async throws -> Int { 0 }
    func redeliver(_ id: String) async throws -> CallRecord { throw APIError.notFound }
    func export(_ format: ExportFormat, agent: String?) async throws -> HistoryExport {
        HistoryExport(filename: "x", data: Data())
    }
    func linkAccount(accountToken: String) async throws -> AccountLink { throw APIError.notFound }
}
