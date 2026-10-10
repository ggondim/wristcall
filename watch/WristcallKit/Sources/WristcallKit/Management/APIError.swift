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
    /// `429`.
    case rateLimited
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
        case .rateLimited: "Too many attempts. Try again in a minute."
        case .unexpectedStatus(let status): "Unexpected reply from the server (\(status))."
        case .network: "Could not reach the server."
        case .malformedResponse: "The server sent a reply the app does not understand."
        }
    }

    /// Maps a non-success reply. The body is `{"error": code, "message": text}`, or FastAPI's
    /// `{"detail": [{"msg": text}]}` for a request it rejected before the route ran.
    public static func error(status: Int, data: Data) -> APIError {
        let body = (try? JSONDecoder().decode(ErrorBody.self, from: data))
        let code = body?.error
        let message = body?.message
        switch status {
        case 401:
            switch code {
            case "invalid_code": return .invalidCode
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
            return code == "not_configured" ? .notConfigured : .notFound
        case 409:
            return .conflict(code: code ?? "conflict", message: message ?? "")
        case 422:
            return .invalid(message ?? body?.detail?.first?.msg ?? "The server rejected the request.")
        case 429:
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
