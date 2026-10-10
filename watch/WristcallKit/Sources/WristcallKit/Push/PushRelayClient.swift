import Foundation

/// APNs environment the device token belongs to.
public enum PushEnvironment: String, Sendable {
    case sandbox, production
}

/// Client of the push relay (the Cloud): `/v1/push/registrations`. Registration is anonymous;
/// the relay answers with a push key that later calls present as a bearer token. The push key
/// is a secret: keep it out of logs.
public struct PushRelayClient: Sendable {
    private let relayURL: URL
    private let http: PushHTTP

    public init(relayURL: URL, session: URLSession = .shared) {
        self.relayURL = relayURL
        self.http = PushHTTP(session: session)
    }

    /// `POST /v1/push/registrations` (`events: ["call.finished"]`); returns the push key.
    /// `tag` comes back in every push, so the watch knows which server a push is about.
    public func register(
        deviceToken: Data,
        topic: String,
        environment: PushEnvironment,
        label: String,
        tag: String
    ) async throws -> String {
        let body = RegistrationBody(
            token: deviceToken.map { String(format: "%02x", $0) }.joined(),
            topic: topic,
            environment: environment.rawValue,
            label: label,
            tag: tag
        )
        let request = try http.request(registrations, method: "POST", json: body)
        let (status, data) = try await http.send(request)
        guard status == 201 else { throw PushHTTP.error(for: status) }
        let reply: RegistrationReply = try http.decode(data)
        return reply.pushKey
    }

    /// `GET /v1/push/registrations/current`: `true` on `200`, `false` on `410` (the relay forgot the key).
    public func isRegistered(pushKey: String) async throws -> Bool {
        let request = try http.request(current, method: "GET", bearer: pushKey)
        let (status, _) = try await http.send(request)
        switch status {
        case 200: return true
        case 410: return false
        default: throw PushHTTP.error(for: status)
        }
    }

    /// `DELETE /v1/push/registrations/current`; `404` counts as done.
    public func unregister(pushKey: String) async throws {
        let request = try http.request(current, method: "DELETE", bearer: pushKey)
        let (status, _) = try await http.send(request)
        guard status == 204 || status == 404 else { throw PushHTTP.error(for: status) }
    }

    private var registrations: URL { relayURL.appending(path: "v1/push/registrations") }
    private var current: URL { registrations.appending(path: "current") }
}

private struct RegistrationBody: Encodable {
    var platform = "apns"
    var token: String
    var topic: String
    var environment: String
    var label: String
    var tag: String
    var events = ["call.finished"]
}

private struct RegistrationReply: Decodable {
    var pushKey: String

    private enum CodingKeys: String, CodingKey {
        case pushKey = "push_key"
    }
}
