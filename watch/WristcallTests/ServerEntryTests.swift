import Foundation
import Testing
import WristcallKit
@testable import Wristcall

struct ServerEntryTests {
    private let credentials = Credentials(
        serverURL: URL(string: "http://127.0.0.1:8765")!, deviceId: "dev_1", token: "tok", id: "srv-1")

    @Test func onlyAnAnsweredServerIsReady() {
        let info = DeviceInfo(deviceId: "dev_1", deviceName: "Watch", profiles: [])
        #expect(ServerEntry(credentials: credentials, status: .ready(info)).isReady)
        #expect(!ServerEntry(credentials: credentials, status: .loading).isReady)
        #expect(!ServerEntry(credentials: credentials, status: .unavailable("down")).isReady)
    }
}
