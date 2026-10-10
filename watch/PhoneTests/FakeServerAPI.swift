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

    // MARK: History

    /// Pages of the history, newest first; a query with `before == nil` gets the first, one with the
    /// previous page's `nextBefore` gets the next, anything else an empty page.
    var pages: [CallPage] = []
    private(set) var queries: [HistoryQuery] = []
    var callsError: (any Error)?
    /// What `call(_:)` answers; falls back to the calls in `pages`.
    var singleCalls: [String: CallRecord] = [:]
    var callError: (any Error)?
    private(set) var deletedCalls: [String] = []
    var deleteCallError: (any Error)?
    /// The `agent` of each `deleteCalls(agent:)` (`nil` = all).
    private(set) var deleteAllRequests: [String?] = []
    var deleteAllResult = 0
    var deleteAllError: (any Error)?
    private(set) var redelivered: [String] = []
    var redeliverResult: CallRecord?
    var redeliverError: (any Error)?
    struct ExportRequest: Equatable {
        var format: ExportFormat
        var agent: String?
        var since: Date?
        var until: Date?
    }
    private(set) var exports: [ExportRequest] = []
    var exportResult = HistoryExport(filename: "wristcall-history.md", data: Data("# History\n".utf8))
    var exportError: (any Error)?

    init(pages: [CallPage] = []) { self.pages = pages }

    convenience init(health: ServerHealth) {
        self.init()
        healthResult = .success(health)
    }

    /// Overrides `pages` for a query (tests that need different answers for different searches).
    var respond: (@Sendable (HistoryQuery) -> CallPage)?
    /// Holds the answer to a query until the returned gate opens: the answer is computed first, so it can
    /// arrive after a newer one (a slow network).
    var hold: (@Sendable (HistoryQuery) -> Gate?)?

    func calls(_ query: HistoryQuery) async throws -> CallPage {
        queries.append(query)
        if let callsError { throw callsError }
        let page = respond?(query) ?? pageFromPages(query)
        if let gate = hold?(query) { await gate.wait() }
        return page
    }

    private func pageFromPages(_ query: HistoryQuery) -> CallPage {
        guard let before = query.before else { return pages.first ?? CallPage(calls: []) }
        guard let index = pages.firstIndex(where: { $0.nextBefore == before }), pages.indices.contains(index + 1)
        else { return CallPage(calls: []) }
        return pages[index + 1]
    }

    func call(_ id: String) async throws -> CallRecord {
        if let callError { throw callError }
        if let found = singleCalls[id] ?? pages.lazy.flatMap(\.calls).first(where: { $0.id == id }) { return found }
        throw APIError.notFound
    }

    func deleteCall(_ id: String) async throws {
        if let deleteCallError { throw deleteCallError }
        deletedCalls.append(id)
    }

    func deleteCalls(agent: String?) async throws -> Int {
        if let deleteAllError { throw deleteAllError }
        deleteAllRequests.append(agent)
        return deleteAllResult
    }

    func redeliver(_ id: String) async throws -> CallRecord {
        redelivered.append(id)
        if let redeliverError { throw redeliverError }
        guard let redeliverResult else { throw APIError.notFound }
        return redeliverResult
    }

    func export(_ format: ExportFormat, agent: String?, since: Date?, until: Date?) async throws -> HistoryExport {
        exports.append(ExportRequest(format: format, agent: agent, since: since, until: until))
        if let exportError { throw exportError }
        return exportResult
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
    var linkResult: Result<AccountLink, any Error> = .failure(APIError.notFound)
    /// What the account links got: the Cloud's token for this server and, for a link by code, the code.
    private(set) var linkRequests: [(serverToken: String, code: String?)] = []

    func linkAccount(serverToken: String) async throws -> AccountLink {
        calls.append("linkAccount")
        linkRequests.append((serverToken, nil))
        return try linkResult.get()
    }

    func linkAccount(serverToken: String, code: PairingCode) async throws -> AccountLink {
        calls.append("linkAccountWithCode")
        linkRequests.append((serverToken, code.digits))
        return try linkResult.get()
    }
}

/// A door a fake answer waits at until the test opens it; `reached()` says an answer is waiting.
actor Gate {
    private var isOpen = false
    private var waiting: [CheckedContinuation<Void, Never>] = []
    private var arrived = 0

    func wait() async {
        arrived += 1
        if isOpen { return }
        await withCheckedContinuation { waiting.append($0) }
    }

    func open() {
        isOpen = true
        waiting.forEach { $0.resume() }
        waiting = []
    }

    /// Returns once an answer is held at the gate (gives up after 5 s so a broken test fails, not hangs).
    func reached() async -> Bool {
        for _ in 0..<1000 {
            if arrived > 0 { return true }
            try? await Task.sleep(for: .milliseconds(5))
        }
        return false
    }
}
