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

    var agentList: [AgentDetail] = []
    var agentsError: (any Error)?
    private(set) var createdAgents: [AgentFields] = []
    private(set) var updatedAgents: [(ref: String, fields: AgentFields)] = []
    private(set) var deletedAgents: [String] = []
    var createAgentError: (any Error)?
    var updateAgentError: (any Error)?
    var deleteAgentError: (any Error)?

    func agents() async throws -> [AgentDetail] {
        calls.append("agents")
        if let agentsError { throw agentsError }
        return agentList
    }

    func agent(_ ref: String) async throws -> AgentDetail {
        guard let found = agentList.first(where: { $0.id == ref || $0.slug == ref }) else { throw APIError.notFound }
        return found
    }

    func createAgent(_ fields: AgentFields) async throws -> AgentDetail {
        calls.append("createAgent")
        if let createAgentError { throw createAgentError }
        createdAgents.append(fields)
        let slug = fields["slug"]?.stringValue ?? "new"
        let created = AgentDetail(id: "ag_\(slug)", slug: slug, displayName: fields["display_name"]?.stringValue ?? slug)
        agentList.append(created)
        return created
    }

    func updateAgent(_ ref: String, _ fields: AgentFields) async throws -> AgentDetail {
        calls.append("updateAgent")
        if let updateAgentError { throw updateAgentError }
        updatedAgents.append((ref, fields))
        guard let index = agentList.firstIndex(where: { $0.id == ref || $0.slug == ref }) else { throw APIError.notFound }
        if let name = fields["display_name"]?.stringValue { agentList[index].displayName = name }
        if let position = fields["position"]?.intValue {
            let moved = agentList.remove(at: index)
            agentList.insert(moved, at: min(position, agentList.count))
            for offset in agentList.indices { agentList[offset].position = offset }
        }
        return agentList.first(where: { $0.id == ref })!
    }

    func deleteAgent(_ ref: String) async throws {
        calls.append("deleteAgent")
        if let deleteAgentError { throw deleteAgentError }
        deletedAgents.append(ref)
        agentList.removeAll { $0.id == ref || $0.slug == ref }
    }

    var providerList = ProviderList(providers: [], customEndpoints: false)
    var providersError: (any Error)?

    func providers() async throws -> ProviderList {
        calls.append("providers")
        if let providersError { throw providersError }
        return providerList
    }

    var deviceList: [DeviceRecord] = []
    var devicesError: (any Error)?
    var revokeError: (any Error)?
    private(set) var revokedDevices: [String] = []
    var grant: PairingCodeGrant?
    var pairingCodeError: (any Error)?
    var requestList: [ApprovalRequest] = []
    var requestsError: (any Error)?
    var approveError: (any Error)?
    var denyError: (any Error)?
    private(set) var approvedRequests: [String] = []
    private(set) var deniedRequests: [String] = []
    /// Runs after the pending requests were read and before they are returned (a tap that lands mid refresh).
    var afterListing: (@Sendable () async -> Void)?

    func devices() async throws -> [DeviceRecord] {
        calls.append("devices")
        if let devicesError { throw devicesError }
        return deviceList
    }

    func revokeDevice(_ id: String) async throws {
        calls.append("revokeDevice")
        if let revokeError { throw revokeError }
        revokedDevices.append(id)
        deviceList.removeAll { $0.id == id }
    }

    func createPairingCode() async throws -> PairingCodeGrant {
        calls.append("createPairingCode")
        if let pairingCodeError { throw pairingCodeError }
        guard let grant else { throw APIError.notFound }
        return grant
    }

    func pairingRequests() async throws -> [ApprovalRequest] {
        calls.append("pairingRequests")
        if let requestsError { throw requestsError }
        let list = requestList
        await afterListing?()
        return list
    }

    func approve(requestID: String) async throws -> String {
        calls.append("approve")
        if let approveError { throw approveError }
        approvedRequests.append(requestID)
        return requestList.first { $0.requestId == requestID }?.deviceName ?? ""
    }

    func deny(requestID: String) async throws {
        calls.append("deny")
        if let denyError { throw denyError }
        deniedRequests.append(requestID)
    }

    func calls(_ query: HistoryQuery) async throws -> CallPage { CallPage(calls: []) }
    func deleteCall(_ id: String) async throws {}
    func deleteCalls(agent: String?) async throws -> Int { 0 }
    func redeliver(_ id: String) async throws -> CallRecord { throw APIError.notFound }
    func export(_ format: ExportFormat, agent: String?) async throws -> HistoryExport {
        HistoryExport(filename: "x", data: Data())
    }
    var pushKeys: [String] = []
    var pushKeyError: (any Error)?
    func setPushKey(_ key: String) async throws {
        calls.append("setPushKey")
        if let pushKeyError { throw pushKeyError }
        pushKeys.append(key)
    }
    func clearPushKey() async throws {
        calls.append("clearPushKey")
        if let pushKeyError { throw pushKeyError }
        pushKeys = []
    }
    func linkAccount(accountToken: String) async throws -> AccountLink { throw APIError.notFound }
}
