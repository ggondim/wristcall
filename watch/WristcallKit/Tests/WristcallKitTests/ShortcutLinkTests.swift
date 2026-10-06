import Foundation
import Testing
import WristcallKit

struct ShortcutLinkTests {
    @Test func theCallLinkIsRecognized() {
        #expect(ShortcutLink.call.absoluteString == "wristcall://call")
        #expect(ShortcutLink.isCall(ShortcutLink.call))
        #expect(ShortcutLink.isCall(URL(string: "WRISTCALL://Call")!))
        #expect(ShortcutLink.isCall(URL(string: "wristcall://call/")!))
    }

    @Test func otherLinksAreNot() {
        #expect(!ShortcutLink.isCall(URL(string: "wristcall://settings")!))
        #expect(!ShortcutLink.isCall(URL(string: "https://call")!))
        #expect(!ShortcutLink.isCall(URL(string: "wristcall:call")!))
    }
}
