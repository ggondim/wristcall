import Foundation
import Network
import os

/// Whether the watch has a network path. `AppModel` asks before starting a call.
@MainActor
protocol NetworkReachability: AnyObject {
    /// `false` when the system reported no usable path; `nil` before its first report.
    var isSatisfied: Bool? { get }
}

/// `NWPathMonitor` for the app's lifetime. Created at launch, so the answer is ready when the
/// user taps "Call".
///
/// On a real watch, low-level networking is allowed only during a call (TN3135), and the path
/// may read `.unsatisfied` between calls even with a working network. So only a path with no
/// interface at all counts as "no network": a wrong "yes" only lets the call fail as before,
/// a wrong "no" would block every call.
@MainActor
final class NetworkPathMonitor: NetworkReachability {
    private(set) var isSatisfied: Bool?

    private let monitor = NWPathMonitor()
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "network")

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            MainActor.assumeIsolated {
                self?.update(path)
            }
        }
        monitor.start(queue: .main)
    }

    private func update(_ path: NWPath) {
        let usable = path.status != .unsatisfied || !path.availableInterfaces.isEmpty
        let interfaces = path.availableInterfaces.map { "\($0.type)" }.joined(separator: ",")
        Self.log.notice(
            "path \(String(describing: path.status), privacy: .public) reason \(String(describing: path.unsatisfiedReason), privacy: .public) interfaces [\(interfaces, privacy: .public)] usable=\(usable)"
        )
        isSatisfied = usable
    }
}
