import Foundation
import Testing
@testable import WristcallKit

struct WatchLinkMessageTests {
    @Test func pairMessagePrintsWithoutTheCode() throws {
        let message = try #require(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://example.com", "code": "12345678", "name": "Home", "expires_at": 1_800_000_000]))
        var dumped = ""
        dump(message, to: &dumped)
        for text in ["\(message)", String(reflecting: message), String(describing: message), dumped] {
            #expect(!text.contains("12345678"))
            #expect(!text.contains("1234 5678"))
            #expect(text.contains("<redacted>"))
            #expect(text.contains("Home"))
        }
        // The dictionary sent to the watch still carries the digits.
        #expect(message.dictionary["code"] as? String == "12345678")
    }

    // MARK: - pair

    @Test func rejectsInsecureServer() {
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "http://example.com", "code": "12345678", "name": "Home", "expires_at": 1_800_000_000]) == nil)
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "http://127.0.0.1:8765", "code": "12345678", "name": "Dev", "expires_at": 1_800_000_000]) != nil)
    }

    @Test func rejectsBadCode() {
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "1234567", "name": "Home", "expires_at": 1_800_000_000]) == nil)
    }

    @Test func roundTrip() {
        let m = WatchLinkMessage.pair(server: URL(string: "https://srv.test")!, code: PairingCode("12345678")!, name: "Home", expiresAt: 1_800_000_000)
        #expect(WatchLinkMessage(m.dictionary) == m)
        #expect(WatchLinkMessage.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000).dictionary["user_code"] as? String == "ZXSG-KCPN")
        #expect(WatchLinkMessage(WatchLinkMessage.refresh.dictionary) == .refresh)
        let device = WatchLinkMessage.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000)
        #expect(WatchLinkMessage(device.dictionary) == device)
    }

    @Test func pairDictionaryHasOnlyAddressCodeNameAndExpiry() {
        let m = WatchLinkMessage.pair(server: URL(string: "https://srv.test/wc")!, code: PairingCode("12345678")!, name: "Home", expiresAt: 1_800_000_000)
        let dictionary = m.dictionary
        #expect(Set(dictionary.keys) == ["v", "type", "server_url", "code", "name", "expires_at"])
        #expect(dictionary["expires_at"] as? Double == 1_800_000_000)
        #expect(dictionary["v"] as? Int == 1)
        #expect(dictionary["type"] as? String == "pair")
        #expect(dictionary["server_url"] as? String == "https://srv.test/wc")
        #expect(dictionary["code"] as? String == "12345678")
        #expect(dictionary["name"] as? String == "Home")
    }

    /// I2: the watch must be able to tell an expired code without asking the server.
    @Test func pairNeedsExpiry() {
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "12345678", "name": "Home"]) == nil)
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "12345678", "name": "Home", "expires_at": "soon"]) == nil)
    }

    @Test func nameIsTrimmedAndCut() {
        let long = String(repeating: "a", count: 80)
        let message = WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "12345678", "name": "  \(long) ", "expires_at": 1_800_000_000])
        #expect(message == .pair(server: URL(string: "https://srv.test")!, code: PairingCode("12345678")!, name: String(repeating: "a", count: 64), expiresAt: 1_800_000_000))
        let sent = WatchLinkMessage.pair(server: URL(string: "https://srv.test")!, code: PairingCode("12345678")!, name: long, expiresAt: 1_800_000_000)
        #expect((sent.dictionary["name"] as? String)?.count == 64)
    }

    @Test func emptyNameIsNil() {
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "12345678", "name": "  ", "expires_at": 1_800_000_000]) == nil)
        #expect(WatchLinkMessage(["v": 1, "type": "pair", "server_url": "https://srv.test", "code": "12345678", "expires_at": 1_800_000_000]) == nil)
    }

    @Test func unknownTypeIsNil() {
        #expect(WatchLinkMessage(["v": 1, "type": "unpair"]) == nil)
        #expect(WatchLinkMessage(["v": 1]) == nil)
        #expect(WatchLinkMessage(["v": 1, "type": 7]) == nil)
    }

    @Test func wrongVersionIsNil() {
        #expect(WatchLinkMessage(["v": 2, "type": "refresh"]) == nil)
        #expect(WatchLinkMessage(["type": "refresh"]) == nil)
        #expect(WatchLinkMessage(["v": "1", "type": "refresh"]) == nil)
        #expect(WatchLinkMessage(["v": 1, "type": "refresh"]) == .refresh)
    }

    // MARK: - device_code

    @Test func userCodeNormalized() {
        let message = WatchLinkMessage(["v": 1, "type": "device_code", "user_code": "zxsgkcpn", "expires_at": 1_800_000_000.0])
        #expect(message == .deviceCode(userCode: "ZXSGKCPN", expiresAt: 1_800_000_000))
        #expect(WatchLinkMessage(["v": 1, "type": "device_code", "user_code": "zxsg-kcpn", "expires_at": 1_800_000_000])
            == .deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000))
    }

    /// A user code built in lowercase is sent uppercased and compares equal to its round trip.
    @Test func userCodeUppercasedWhenBuilt() {
        let lower = WatchLinkMessage.deviceCode(userCode: "zxsg-kcpn", expiresAt: 1_800_000_000)
        #expect(lower.dictionary["user_code"] as? String == "ZXSG-KCPN")
        #expect(WatchLinkMessage(lower.dictionary) == lower)
        #expect(lower == .deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000))
    }

    @Test func badUserCodeIsNil() {
        for code in ["ZXSG-KCP", "ZXSG--KCPN", "ZXSG KCPN", "ZXSG-KCPN1", "ZXSÉ-KCPN", ""] {
            #expect(WatchLinkMessage(["v": 1, "type": "device_code", "user_code": code, "expires_at": 1_800_000_000]) == nil)
        }
    }

    @Test func deviceCodeNeedsExpiry() {
        #expect(WatchLinkMessage(["v": 1, "type": "device_code", "user_code": "ZXSG-KCPN"]) == nil)
        #expect(WatchLinkMessage.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000).dictionary["expires_at"] as? Double == 1_800_000_000)
    }

    /// Review Focus I4: the poll secret of the device flow never crosses WatchConnectivity.
    @Test func signedInRoundTrip() {
        let dictionary = WatchLinkMessage.signedIn.dictionary
        #expect(dictionary["type"] as? String == "signed_in")
        #expect(dictionary.count == 2)
        #expect(WatchLinkMessage(dictionary) == .signedIn)
        #expect(WatchLinkMessage.signedIn != .refresh)
    }

    @Test func deviceCodeNeverLeavesWatch() {
        let dictionary = WatchLinkMessage.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_800_000_000).dictionary
        #expect(dictionary["device_code"] == nil)
        #expect(Set(dictionary.keys) == ["v", "type", "user_code", "expires_at"])
    }

    // MARK: - Reply and context

    @Test func replyRoundTrip() {
        #expect(WatchLinkReply(WatchLinkReply(ok: true).dictionary) == WatchLinkReply(ok: true))
        #expect(WatchLinkReply(WatchLinkReply(ok: false, error: "busy").dictionary) == WatchLinkReply(ok: false, error: "busy"))
        #expect(WatchLinkReply(ok: true).dictionary["error"] == nil)
        #expect(WatchLinkReply(["error": "busy"]) == nil)
    }

    /// I3: a server that wants the owner's approval: the watch says so at once and keeps waiting.
    @Test func pendingReplyRoundTrip() {
        let reply = WatchLinkReply(ok: true, pending: true, requestId: "4821")
        #expect(WatchLinkReply(reply.dictionary) == reply)
        #expect(reply.dictionary["pending"] as? Bool == true)
        #expect(reply.dictionary["request_id"] as? String == "4821")
        #expect(WatchLinkReply(["ok": true]) == WatchLinkReply(ok: true))
        #expect(WatchLinkReply(ok: true).dictionary["pending"] == nil)
        // Only a 4 digit request id is taken.
        #expect(WatchLinkReply(["ok": true, "pending": true, "request_id": "48 21"]) == WatchLinkReply(ok: true, pending: true))
    }

    @Test func contextRoundTrip() {
        let context = WatchLinkContext(servers: ["https://srv.test", "http://127.0.0.1:8765"])
        #expect(WatchLinkContext(context.dictionary) == context)
        #expect(WatchLinkContext([:]) == nil)
        #expect(WatchLinkContext(["v": 1, "servers": [1, 2]]) == nil)
        #expect(WatchLinkContext(["v": 2, "servers": ["https://srv.test"]]) == nil)
        #expect(WatchLinkContext(WatchLinkContext(servers: []).dictionary) == WatchLinkContext(servers: []))
    }
}
