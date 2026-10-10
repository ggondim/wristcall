import Foundation

/// The HTTP plumbing of the management client. Like `PushHTTP`, but errors are `APIError`.
struct APIHTTP: Sendable {
    let session: URLSession
    var timeout: TimeInterval = 15

    func request(_ url: URL, method: String, bearer: String? = nil, json: (any Encodable)? = nil,
                 query: [URLQueryItem] = []) throws -> URLRequest {
        var target = url
        if !query.isEmpty {
            guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
                throw APIError.malformedResponse
            }
            components.queryItems = query
            // URLComponents leaves "+" as is in a query, and servers decode it as a space.
            if let encoded = components.percentEncodedQuery {
                components.percentEncodedQuery = encoded.replacingOccurrences(of: "+", with: "%2B")
            }
            guard let withQuery = components.url else { throw APIError.malformedResponse }
            target = withQuery
        }
        var request = URLRequest(url: target, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
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

    /// `headers` has lowercased names.
    func send(_ request: URLRequest) async throws -> (status: Int, data: Data, headers: [String: String]) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw APIError.malformedResponse }
            var headers: [String: String] = [:]
            for (name, value) in http.allHeaderFields {
                if let name = name as? String, let value = value as? String {
                    headers[name.lowercased()] = value
                }
            }
            return (http.statusCode, data, headers)
        } catch let error as URLError {
            throw APIError.network(error.code)
        }
    }

    /// Models spell their snake_case keys out with `CodingKeys`: a key strategy would also rewrite the
    /// keys inside `JSONValue` objects (endpoint options such as `base_url`).
    func decode<T: Decodable>(_ data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.malformedResponse
        }
    }

    static func error(status: Int, data: Data) -> APIError {
        APIError.error(status: status, data: data)
    }
}
