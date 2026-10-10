import Foundation
import WristcallKit

extension CallRecord {
    /// A delivered one-shot call made at `at` (Unix seconds); override what a test cares about.
    static func sample(
        id: String = "c1",
        at: Double = 100,
        callType: String = "one-shot",
        status: String = "delivered",
        error: String? = nil,
        text: String? = "buy milk",
        attempts: Int = 1,
        agent: AgentRef? = AgentRef(id: "ag_notes", slug: "notes", displayName: "Notes"),
        entries: [CallEntry] = []
    ) -> CallRecord {
        CallRecord(
            id: id, agentId: agent?.id, callType: callType, status: status, error: error, text: text,
            attempts: attempts, lastHttpStatus: status == "delivered" ? 200 : nil, createdAt: at,
            agent: agent, entries: entries
        )
    }
}
