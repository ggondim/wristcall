import Foundation

/// The history routes of a server (`/v1/calls`, `docs/protocol.md`), with a personal token: a device token is
/// refused with `403` (it reads only its own call, through `CallStatusPoller`).
public struct HistoryClient: Sendable {
    private let server: URL
    private let token: String
    private let http: APIHTTP

    public init(server: URL, token: String, session: URLSession = .shared, timeout: TimeInterval = 30) {
        self.server = server
        self.token = token
        self.http = APIHTTP(session: session, timeout: timeout)
    }

    /// `GET /v1/calls`.
    public func calls(_ query: HistoryQuery) async throws -> CallPage {
        var items = Self.filterItems(agent: query.agent, since: query.since, until: query.until)
        if let text = query.text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
            items.append(URLQueryItem(name: "q", value: text))
        }
        if let before = query.before {
            items.append(URLQueryItem(name: "before", value: before))
        }
        let limit = min(max(query.limit, 1), HistoryQuery.maxLimit)
        items.append(URLQueryItem(name: "limit", value: String(limit)))
        return try await http.run(request(calls, "GET", query: items), expecting: 200)
    }

    /// `GET /v1/calls/{id}`.
    public func call(_ id: String) async throws -> CallRecord {
        try await http.run(request(callURL(id), "GET"), expecting: 200)
    }

    /// `DELETE /v1/calls/{id}`.
    public func deleteCall(_ id: String) async throws {
        let (status, data, _) = try await http.send(request(callURL(id), "DELETE"))
        guard status == 204 else { throw APIHTTP.error(status: status, data: data) }
    }

    /// `DELETE /v1/calls?agent=…`, or `?all=true` for `agent == nil`: how many calls were deleted.
    public func deleteCalls(agent: String?) async throws -> Int {
        let item = agent.map { URLQueryItem(name: "agent", value: $0) } ?? URLQueryItem(name: "all", value: "true")
        let reply: Deleted = try await http.run(request(calls, "DELETE", query: [item]), expecting: 200)
        return reply.deleted
    }

    /// `POST /v1/calls/{id}/redeliver`: the call, now `processing`. A call that cannot be redelivered is
    /// `APIError.conflict` with the reason as `code` (`not_failed`, `no_text`, `busy`, `agent_gone`, ...).
    public func redeliver(_ id: String) async throws -> CallRecord {
        try await http.run(request(callURL(id).appending(component: "redeliver"), "POST"), expecting: 202)
    }

    /// `GET /v1/calls/export`: the file the server builds from the filtered history.
    public func export(_ format: ExportFormat, agent: String? = nil, since: Date? = nil,
                       until: Date? = nil) async throws -> HistoryExport {
        let items = [URLQueryItem(name: "format", value: format.rawValue)]
            + Self.filterItems(agent: agent, since: since, until: until)
        var request = try request(calls.appending(component: "export"), "GET", query: items)
        request.setValue("*/*", forHTTPHeaderField: "Accept")
        let (status, data, headers) = try await http.send(request)
        guard status == 200 else { throw APIHTTP.error(status: status, data: data) }
        let fallback = "wristcall-history.\(format.rawValue)"
        return HistoryExport(filename: Self.filename(from: headers["content-disposition"], fallback: fallback), data: data)
    }

    // MARK: - Plumbing

    private var calls: URL { server.appending(path: "v1/calls") }

    private func callURL(_ id: String) -> URL { calls.appending(component: id) }

    private func request(_ url: URL, _ method: String, query: [URLQueryItem] = []) throws -> URLRequest {
        try http.request(url, method: method, bearer: token, query: query)
    }

    private static func filterItems(agent: String?, since: Date?, until: Date?) -> [URLQueryItem] {
        var items: [URLQueryItem] = []
        if let agent { items.append(URLQueryItem(name: "agent", value: agent)) }
        if let since { items.append(URLQueryItem(name: "since", value: seconds(since))) }
        if let until { items.append(URLQueryItem(name: "until", value: seconds(until))) }
        return items
    }

    /// Whole Unix seconds, rounded down.
    private static func seconds(_ date: Date) -> String {
        String(Int(date.timeIntervalSince1970.rounded(.down)))
    }

    /// The `filename` parameter of a `Content-Disposition` header, safe to use as a local file name: only its
    /// last path component, only `[A-Za-z0-9._-]`, never starting with a dot. `fallback` when nothing is left.
    static func filename(from header: String?, fallback: String) -> String {
        guard let raw = filenameParameter(in: header) else { return fallback }
        let last = raw.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "/" || $0 == "\\" }).last ?? ""
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        let clean = String(last.filter { allowed.contains($0) }.drop { $0 == "." }.prefix(128))
        return clean.isEmpty ? fallback : clean
    }

    /// The value of `filename="…"` (or an unquoted `filename=…`); `filename*=` is ignored.
    private static func filenameParameter(in header: String?) -> String? {
        guard let header else { return nil }
        var search = header.startIndex..<header.endIndex
        while let found = header.range(of: "filename=", options: .caseInsensitive, range: search) {
            let boundary = found.lowerBound == header.startIndex
                || [";", " ", "\t"].contains(header[header.index(before: found.lowerBound)])
            if boundary {
                let rest = header[found.upperBound...]
                if rest.hasPrefix("\"") {
                    return String(rest.dropFirst().prefix { $0 != "\"" })
                }
                return String(rest.prefix { $0 != ";" }).trimmingCharacters(in: .whitespaces)
            }
            search = found.upperBound..<header.endIndex
        }
        return nil
    }

    private struct Deleted: Decodable { var deleted: Int }
}

extension HistoryClient: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String { "HistoryClient(server: \(server), token: <redacted>)" }
    public var debugDescription: String { description }
}
