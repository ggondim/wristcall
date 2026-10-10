import Foundation

/// The little HTTP plumbing the push clients share. Errors are `PairingError`, like the other clients.
struct PushHTTP: Sendable {
    let session: URLSession
    var timeout: TimeInterval = 15

    func request(_ url: URL, method: String, bearer: String? = nil, json: (any Encodable)? = nil) throws -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let bearer {
            request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        }
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(json)
        }
        return request
    }

    func send(_ request: URLRequest) async throws -> (status: Int, data: Data) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw PairingError.malformedResponse }
            return (http.statusCode, data)
        } catch let error as URLError {
            throw PairingError.network(error.code)
        }
    }

    func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw PairingError.malformedResponse
        }
    }

    /// Statuses every route shares; route-specific ones are handled before calling this.
    static func error(for status: Int) -> PairingError {
        switch status {
        case 401: .unauthorized
        case 404: .notFound
        case 422: .invalidRequest
        case 429: .rateLimited
        default: .unexpectedStatus(status)
        }
    }
}
