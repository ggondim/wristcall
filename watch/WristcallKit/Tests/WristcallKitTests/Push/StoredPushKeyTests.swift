import Foundation
import Testing
import WristcallKit

struct StoredPushKeyTests {
    @Test func descriptionHidesTheKey() throws {
        let key = StoredPushKey(pushKey: "wc_push_secret", deviceToken: "0a0b0c")
        for text in [String(describing: key), String(reflecting: key), dumped(key)] {
            #expect(!text.contains("wc_push_secret"))
            #expect(!text.contains("0a0b0c"))
        }
        // The stored form (Keychain item) is the one the watch 0.3.0 wrote.
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(key)) as? [String: String]
        #expect(json == ["pushKey": "wc_push_secret", "deviceToken": "0a0b0c"])
    }
}
