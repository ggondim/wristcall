import Foundation

/// A device the server just paired (flow A, or flow B after approval).
public struct PairedDevice: Sendable, Equatable {
    public var deviceId: String
    /// Bearer token for `/v1/me` and `/v1/call`. Store it with a `CredentialStore`, never log it.
    public var token: String

    public init(deviceId: String, token: String) {
        self.deviceId = deviceId
        self.token = token
    }
}

/// A pairing request waiting for the owner's approval (flow B).
public struct PairingRequest: Sendable, Equatable {
    /// 4 digits to show on screen; the owner runs `wristcall devices approve <requestId>`.
    public var requestId: String
    /// Client secret for `poll(server:pollToken:)`. Never show it or log it.
    public var pollToken: String
    public var expiresAt: Date

    public init(requestId: String, pollToken: String, expiresAt: Date) {
        self.requestId = requestId
        self.pollToken = pollToken
        self.expiresAt = expiresAt
    }
}

/// Reply to `POST /v1/pair`.
public enum PairResult: Sendable, Equatable {
    /// `200`: paired (flow A).
    case paired(PairedDevice)
    /// `202`: waiting for approval (flow B); poll every `PairingClient.pollInterval`.
    case pending(PairingRequest)
}

/// Reply to `POST /v1/pair/poll`.
public enum PollResult: Sendable, Equatable {
    /// `202`: still waiting for the owner.
    case pending(requestId: String)
    /// `200`: approved. The server delivers the token only once.
    case paired(PairedDevice)
    /// `410`: expired or already delivered; start over.
    case gone
}

/// Reply to `GET /v1/me`.
public struct DeviceInfo: Sendable, Equatable, Decodable {
    public var deviceId: String
    public var deviceName: String
    /// The account behind the device; `nil` from servers before 0.5.0.
    public var user: UserInfo?
    /// What the user can call, in the watch's order. A 0.2.x server sends only `profiles`:
    /// each one becomes a conversation agent (`Agent(profile:)`).
    public var agents: [Agent]
    /// The 0.2.x list (`slug` and `display_name` of each agent), kept for code that predates agents.
    public var profiles: [Profile]

    /// For a 0.2.x server: the agents are derived from `profiles`.
    public init(deviceId: String, deviceName: String, profiles: [Profile]) {
        self.deviceId = deviceId
        self.deviceName = deviceName
        self.user = nil
        self.agents = profiles.map(Agent.init(profile:))
        self.profiles = profiles
    }

    /// The profiles are derived from the agents' `slug` and `displayName`, as the server does.
    public init(deviceId: String, deviceName: String, user: UserInfo?, agents: [Agent]) {
        self.deviceId = deviceId
        self.deviceName = deviceName
        self.user = user
        self.agents = agents
        self.profiles = agents.map { Profile(name: $0.slug, displayName: $0.displayName) }
    }

    private enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case deviceName = "device_name"
        case user
        case agents
        case profiles
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let deviceId = try container.decode(String.self, forKey: .deviceId)
        let deviceName = try container.decode(String.self, forKey: .deviceName)
        let agents = try container.decodeIfPresent([Agent].self, forKey: .agents)
        let profiles = try container.decodeIfPresent([Profile].self, forKey: .profiles)
        if let agents {
            self.init(deviceId: deviceId, deviceName: deviceName, user: try container.decodeIfPresent(UserInfo.self, forKey: .user), agents: agents)
            if let profiles {
                self.profiles = profiles
            }
        } else if let profiles {
            self.init(deviceId: deviceId, deviceName: deviceName, profiles: profiles)
            user = try container.decodeIfPresent(UserInfo.self, forKey: .user)
        } else {
            throw DecodingError.keyNotFound(CodingKeys.profiles, .init(codingPath: decoder.codingPath, debugDescription: "neither agents nor profiles"))
        }
    }
}

/// Everything that can go wrong in pairing and in `/v1/me`.
public enum PairingError: Error, Sendable, Equatable {
    /// `401` on `POST /v1/pair`: wrong, expired or used code (in manual mode also: too many pending requests).
    case invalidCode
    /// `429`.
    case rateLimited
    /// `422`: the server rejected the request body.
    case invalidRequest
    /// `401` on `/v1/me`: the token was revoked or never existed. Delete the credentials.
    case unauthorized
    /// The directory answered `404` to the first try and to all retries.
    case codeNotFound
    /// The directory pointed to a URL that is not `https://host`.
    case insecureServerURL
    /// A status the protocol does not define for this route.
    case unexpectedStatus(Int)
    /// A defined status with a body that does not match the protocol.
    case malformedResponse
    /// No HTTP reply at all (offline, DNS, TLS, timeout).
    case network(URLError.Code)
}

// The poll token and the device token are secrets: keep them out of `print`, logs and test failures.

extension PairedDevice: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String { "PairedDevice(deviceId: \(deviceId), token: <redacted>)" }
    public var debugDescription: String { description }
}

extension PairingRequest: CustomStringConvertible, CustomDebugStringConvertible {
    public var description: String {
        "PairingRequest(requestId: \(requestId), pollToken: <redacted>, expiresAt: \(expiresAt))"
    }

    public var debugDescription: String { description }
}
