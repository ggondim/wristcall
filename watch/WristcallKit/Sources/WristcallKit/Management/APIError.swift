import Foundation

/// Errors of every management and account route. `message` is a short text for the UI.
public enum APIError: Error, Sendable, Equatable {
    /// `401` (`unauthorized`, or a body the client does not know): the token is revoked or never existed.
    case unauthorized
    /// `401 invalid_code`: wrong, expired or used pairing code.
    case invalidCode
    /// `401 invalid_account_token`: the server did not accept the Cloud token.
    case invalidAccountToken
    /// `403 forbidden`: a device token on an owner route.
    case forbidden(String)
    /// `403 limit`: the user reached the device limit.
    case limit(String)
    /// `403 not_linked`: the account is not linked to a user on that server.
    case notLinked(String)
    /// `404 not_found`, or a `404` without a body.
    case notFound
    /// `404 not_configured`: the server has no central account.
    case notConfigured
    /// `409`, with the server's `error` code and `message`.
    case conflict(code: String, message: String)
    /// `422`: the `message` of the error body, or the first `detail[].msg` of the FastAPI validation reply.
    case invalid(String)
    /// `401 revoked`: the session or token was revoked on purpose.
    case revoked
    /// `401 stale_token`: the token is too old for a sensitive action; sign in again.
    case staleToken
    /// `429`.
    case rateLimited
    /// `429` with a `Retry-After` (seconds): try again after that long.
    case retryLater(TimeInterval)
    /// `502` and `503` (`account_unavailable`, `directory`).
    case unavailable(String)
    /// A status the API does not define for the route.
    case unexpectedStatus(Int)
    /// No HTTP reply at all (offline, DNS, TLS, timeout).
    case network(URLError.Code)
    /// A defined status with a body that does not match the API.
    case malformedResponse

    public var message: String {
        switch self {
        case .unauthorized: "The token was not accepted. It may have been revoked."
        case .invalidCode: "The code is wrong, expired or already used."
        case .invalidAccountToken: "The server did not accept the account login."
        case .forbidden(let text), .limit(let text), .notLinked(let text), .invalid(let text), .unavailable(let text):
            text.isEmpty ? "The server refused the request." : text
        case .notFound: "Not found."
        case .notConfigured: "This server has no account set up."
        case .conflict(_, let text): text.isEmpty ? "The server reported a conflict." : text
        case .revoked: "This login was revoked. Sign in again."
        case .staleToken: "The login is too old for this action. Sign in again."
        case .rateLimited: "Too many attempts. Try again in a minute."
        case .retryLater(let seconds): Self.retryMessage(seconds)
        case .unexpectedStatus(let status): "Unexpected reply from the server (\(status))."
        case .network: "Could not reach the server."
        case .malformedResponse: "The server sent a reply the app does not understand."
        }
    }

    private static func retryMessage(_ seconds: TimeInterval) -> String {
        // Clamped first: a hostile or broken `Retry-After` (1e30) must not overflow the Int conversion.
        let safe = seconds.isFinite ? min(max(seconds, 0), 86_400) : 86_400
        let minutes = max(1, Int((safe / 60).rounded(.up)))
        return "Too many attempts. Try again in \(minutes) \(minutes == 1 ? "minute" : "minutes")."
    }

    /// Maps a non-success reply. The body is `{"error": code, "message": text}`, or FastAPI's
    /// `{"detail": [{"msg": text}]}` for a request it rejected before the route ran.
    public static func error(status: Int, data: Data) -> APIError {
        error(status: status, data: data, headers: [:])
    }

    /// Same, reading `Retry-After` from `headers` (lowercased names, as `APIHTTP.send` returns them).
    public static func error(status: Int, data: Data, headers: [String: String]) -> APIError {
        let body = (try? JSONDecoder().decode(ErrorBody.self, from: data))
        let code = body?.error
        let message = body?.message
        switch status {
        case 401:
            switch code {
            case "invalid_code": return .invalidCode
            case "revoked": return .revoked
            case "stale_token": return .staleToken
            case "invalid_account_token": return .invalidAccountToken
            default: return .unauthorized
            }
        case 403:
            switch code {
            case "limit": return .limit(message ?? "The device limit was reached.")
            case "not_linked": return .notLinked(message ?? "The account is not linked to a user on this server.")
            default: return .forbidden(message ?? "Not allowed with this token.")
            }
        case 404:
            switch code {
            case "not_configured": return .notConfigured
            case "invalid_code": return .invalidCode
            default: return .notFound
            }
        case 409:
            return .conflict(code: code ?? "conflict", message: message ?? "")
        case 422:
            return .invalid(message ?? body?.detail?.first?.msg ?? "The server rejected the request.")
        case 429:
            if let seconds = headers["retry-after"].flatMap({ TimeInterval($0.trimmingCharacters(in: .whitespaces)) }),
               seconds.isFinite, seconds >= 0
            {
                return .retryLater(seconds)
            }
            return .rateLimited
        case 502, 503:
            return .unavailable(message ?? "The server is unavailable. Try again later.")
        default:
            return .unexpectedStatus(status)
        }
    }

    private struct ErrorBody: Decodable {
        var error: String?
        var message: String?
        var detail: [Detail]?

        struct Detail: Decodable { var msg: String? }

        init(from decoder: any Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            error = try? container.decodeIfPresent(String.self, forKey: .error)
            message = try? container.decodeIfPresent(String.self, forKey: .message)
            // FastAPI sends a list; some handlers send a plain string: neither may fail the whole decode.
            detail = try? container.decodeIfPresent([Detail].self, forKey: .detail)
        }

        private enum CodingKeys: String, CodingKey { case error, message, detail }
    }
}
