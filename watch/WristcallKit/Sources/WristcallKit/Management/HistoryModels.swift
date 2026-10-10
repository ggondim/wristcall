import Foundation

/// One utterance of a call, as `GET /v1/calls` shows it. `text` is nil when `error` says why it is missing
/// (`stt_failed`, `responder_failed`, `tts_failed`, `unreadable`).
public struct CallEntry: Decodable, Sendable, Equatable {
    public var role: String
    public var text: String?
    public var error: String?
    public var at: Double

    public init(role: String, text: String? = nil, error: String? = nil, at: Double) {
        self.role = role
        self.text = text
        self.error = error
        self.at = at
    }
}

/// A call of the history (`server/src/wristcall/history.py`, `call_detail`). Times are Unix seconds.
public struct CallRecord: Decodable, Sendable, Equatable, Identifiable {
    public var id: String
    public var agentId: String?
    /// `conversation`, `one-shot` or `monologue`.
    public var callType: String
    /// `processing`, `completed`, `failed`, ... (the server's call status).
    public var status: String
    public var error: String?
    /// What the user said, joined: the transcript a one-way call delivers.
    public var text: String?
    public var attempts: Int
    public var lastHttpStatus: Int?
    public var createdAt: Double
    public var endedAt: Double?
    public var finishedAt: Double?
    /// When the retention deletes the call; nil keeps it until the user does.
    public var expiresAt: Double?
    /// The agent as it was when called (it may have been renamed or deleted since).
    public var agent: AgentRef?
    public var entries: [CallEntry]

    public struct AgentRef: Decodable, Sendable, Equatable {
        public var id: String
        public var slug: String
        public var displayName: String

        public init(id: String, slug: String, displayName: String) {
            self.id = id
            self.slug = slug
            self.displayName = displayName
        }

        private enum CodingKeys: String, CodingKey {
            case id, slug
            case displayName = "display_name"
        }
    }

    /// What `/redeliver` accepts (`server/src/wristcall/redelivery.py`): a one-shot or monologue call whose delivery
    /// failed (`delivery_failed`) or was cut short (`interrupted`), with a transcript. The server decides in the end.
    public var canRedeliver: Bool {
        guard callType == "one-shot" || callType == "monologue", status == "failed",
              let error, Self.redeliverableErrors.contains(error),
              let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return false }
        return true
    }

    private static let redeliverableErrors: Set<String> = ["delivery_failed", "interrupted"]

    public init(id: String, agentId: String? = nil, callType: String, status: String, error: String? = nil,
                text: String? = nil, attempts: Int = 0, lastHttpStatus: Int? = nil, createdAt: Double,
                endedAt: Double? = nil, finishedAt: Double? = nil, expiresAt: Double? = nil,
                agent: AgentRef? = nil, entries: [CallEntry] = []) {
        self.id = id
        self.agentId = agentId
        self.callType = callType
        self.status = status
        self.error = error
        self.text = text
        self.attempts = attempts
        self.lastHttpStatus = lastHttpStatus
        self.createdAt = createdAt
        self.endedAt = endedAt
        self.finishedAt = finishedAt
        self.expiresAt = expiresAt
        self.agent = agent
        self.entries = entries
    }

    private enum CodingKeys: String, CodingKey {
        case id, status, error, text, attempts, agent, entries
        case agentId = "agent_id"
        case callType = "call_type"
        case lastHttpStatus = "last_http_status"
        case createdAt = "created_at"
        case endedAt = "ended_at"
        case finishedAt = "finished_at"
        case expiresAt = "expires_at"
    }
}

/// One page of `GET /v1/calls`, newest first. Ask again with `HistoryQuery.before = nextBefore` for the next
/// page; `nextBefore` is nil on the last one.
public struct CallPage: Decodable, Sendable, Equatable {
    public var calls: [CallRecord]
    public var nextBefore: String?

    public init(calls: [CallRecord], nextBefore: String? = nil) {
        self.calls = calls
        self.nextBefore = nextBefore
    }

    private enum CodingKeys: String, CodingKey {
        case calls
        case nextBefore = "next_before"
    }
}

/// Filters of the list (and of the export). `agent` is an id or a slug; `text` is a search over what was said.
public struct HistoryQuery: Sendable, Equatable {
    public static let maxLimit = 100

    public var agent: String?
    public var text: String?
    public var since: Date?
    public var until: Date?
    public var before: String?
    /// Page size; the client keeps it between 1 and `maxLimit`.
    public var limit: Int

    public init(agent: String? = nil, text: String? = nil, since: Date? = nil, until: Date? = nil,
                before: String? = nil, limit: Int = 30) {
        self.agent = agent
        self.text = text
        self.since = since
        self.until = until
        self.before = before
        self.limit = limit
    }
}

public enum ExportFormat: String, Sendable {
    case markdown = "md"
    case json
}

/// A file the server built (`GET /v1/calls/export`): its name and bytes, ready for a share sheet.
public struct HistoryExport: Sendable {
    public var filename: String
    public var data: Data

    public init(filename: String, data: Data) {
        self.filename = filename
        self.data = data
    }
}
