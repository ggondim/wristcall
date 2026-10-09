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

    /// What a complication with no agent chosen opens (decision W20): the app, never a call.
    @Test func theOpenLinkIsNotACall() {
        #expect(ShortcutLink.open.absoluteString == "wristcall://open")
        #expect(ShortcutLink.open.scheme == ShortcutLink.scheme)
        #expect(!ShortcutLink.isCall(ShortcutLink.open))
    }

    @Test func theLinkWithoutAgentIsTheOldOne() {
        #expect(ShortcutLink.call(agent: nil) == ShortcutLink.call)
        #expect(ShortcutLink.agent(in: ShortcutLink.call) == nil)
    }

    @Test func theLinkCarriesTheAgent() {
        let url = ShortcutLink.call(agent: "srv-1/ag_one")
        #expect(ShortcutLink.isCall(url))
        #expect(ShortcutLink.agent(in: url) == "srv-1/ag_one")
    }

    @Test func theAgentIsEscapedAndComesBackIdentical() {
        let text = "a b&c=d?e#f/ág+é"
        let url = ShortcutLink.call(agent: text)
        #expect(ShortcutLink.isCall(url))
        #expect(!url.absoluteString.contains(" "))
        #expect(!url.absoluteString.contains("&c="))
        #expect(ShortcutLink.agent(in: url) == text)
    }

    @Test func theAgentIsKeptRawAndTheParameterNameIgnoresLetterCase() {
        #expect(ShortcutLink.agent(in: URL(string: "WRISTCALL://Call?AGENT=Srv-1/AG_One")!) == "Srv-1/AG_One")
        #expect(ShortcutLink.agent(in: URL(string: "wristcall://call?agent=not%20valid")!) == "not valid")
    }

    @Test func aMissingOrEmptyAgentIsNil() {
        #expect(ShortcutLink.agent(in: URL(string: "wristcall://call?agent=")!) == nil)
        #expect(ShortcutLink.agent(in: URL(string: "wristcall://call?other=1")!) == nil)
    }
}
