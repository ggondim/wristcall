import Network
import Testing
@testable import Wristcall

struct NetworkReachabilityTests {
    /// Only a path that is unsatisfied and has no interface at all blocks a call: on a real watch
    /// the path may read unsatisfied between calls with a working network (TN3135).
    @Test(arguments: [
        (NWPath.Status.satisfied, 1, true),
        (.satisfied, 0, true),
        (.requiresConnection, 0, true),
        (.unsatisfied, 1, true),
        (.unsatisfied, 0, false),
    ])
    func onlyAnUnsatisfiedPathWithoutInterfacesIsUnusable(status: NWPath.Status, interfaceCount: Int, usable: Bool) {
        #expect(NetworkPathMonitor.isUsable(status: status, interfaceCount: interfaceCount) == usable)
    }
}
