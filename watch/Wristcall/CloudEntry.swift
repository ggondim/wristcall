import Foundation
import Synchronization
import WristcallKit

/// The Wristcall Cloud's agenda entry (`kind == "cloud"`) as a virtual server in `AppModel.servers`: its agents
/// go in the grid and the catalog like any server's, but it is never paired, never in the Keychain, never
/// published to the iPhone nor registered for push. Calls to it go through a Cloud route, not a device token.
extension Credentials {
    /// Its fixed local id: complications and shortcuts point to `cloud/ag_…`.
    static let cloudEntryID = "cloud"

    /// No device and no token: the Cloud session authenticates what goes to it.
    static func cloudEntry(url: URL) -> Credentials {
        Credentials(serverURL: url, deviceId: "", token: "", id: cloudEntryID)
    }

    var isCloudEntry: Bool { id == Self.cloudEntryID }
}

extension DeviceInfo {
    /// What the grid shows for the cloud entry: its agents from the agenda, no `/v1/me` asked.
    init(cloud entry: CloudServer) {
        self.init(deviceId: "", deviceName: entry.name, user: nil, agents: entry.agents.map(Agent.init(cloud:)))
    }
}

/// The last cloud entry the agenda gave, so its agents show at launch without network. Not a secret (no token).
protocol CloudEntryStore {
    func load() -> CloudServer?
    /// `nil` removes it.
    func save(_ entry: CloudServer?)
}

/// JSON in `UserDefaults`, key `cloudEntry`.
struct DefaultsCloudEntryStore: CloudEntryStore {
    static let key = "cloudEntry"

    let defaults: UserDefaults

    func load() -> CloudServer? {
        defaults.data(forKey: Self.key).flatMap { try? JSONDecoder().decode(CloudServer.self, from: $0) }
    }

    func save(_ entry: CloudServer?) {
        if let entry, let data = try? JSONEncoder().encode(entry) {
            defaults.set(data, forKey: Self.key)
        } else {
            defaults.removeObject(forKey: Self.key)
        }
    }
}

/// For tests and SwiftUI previews.
final class InMemoryCloudEntryStore: CloudEntryStore, Sendable {
    private let stored: Mutex<CloudServer?>

    init(_ entry: CloudServer? = nil) {
        stored = Mutex(entry)
    }

    func load() -> CloudServer? {
        stored.withLock { $0 }
    }

    func save(_ entry: CloudServer?) {
        stored.withLock { $0 = entry }
    }
}
