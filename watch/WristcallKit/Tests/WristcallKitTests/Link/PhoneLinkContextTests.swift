import Foundation
import Testing
@testable import WristcallKit

struct PhoneLinkContextTests {
    @Test func roundTripsAndRejectsOtherVersions() throws {
        let context = PhoneLinkContext(enrollCode: "wce_CODE", expiresAt: 1_800_000_000.5)
        #expect(context.dictionary["v"] as? Int == 1)
        #expect(context.dictionary["enroll_code"] as? String == "wce_CODE")
        #expect(context.dictionary["expires_at"] as? Double == 1_800_000_000.5)
        #expect(PhoneLinkContext(context.dictionary) == context)

        // A plist may hand the expiry back as an integer.
        #expect(PhoneLinkContext(["v": 1, "enroll_code": "wce_CODE", "expires_at": 1_800_000_000])?.expiresAt == 1_800_000_000)

        #expect(PhoneLinkContext(["v": 2, "enroll_code": "wce_CODE", "expires_at": 1.0]) == nil)
        #expect(PhoneLinkContext(["enroll_code": "wce_CODE", "expires_at": 1.0]) == nil)
        #expect(PhoneLinkContext(["v": 1, "expires_at": 1.0]) == nil)
        #expect(PhoneLinkContext(["v": 1, "enroll_code": "", "expires_at": 1.0]) == nil)
        #expect(PhoneLinkContext(["v": 1, "enroll_code": "wce_CODE"]) == nil)
        #expect(PhoneLinkContext(["v": 1, "enroll_code": 42, "expires_at": 1.0]) == nil)
    }

    @Test func accountIsOptional() {
        let without = PhoneLinkContext(enrollCode: "wce_CODE", expiresAt: 1_800_000_000)
        #expect(without.account == nil)
        #expect(without.dictionary["account"] == nil)
        #expect(PhoneLinkContext(without.dictionary) == without)

        let with = PhoneLinkContext(enrollCode: "wce_CODE", expiresAt: 1_800_000_000, account: "acc_1")
        #expect(with.dictionary["account"] as? String == "acc_1")
        #expect(PhoneLinkContext(with.dictionary) == with)
        #expect(PhoneLinkContext(with.dictionary)?.account == "acc_1")
        #expect(with != without)
        #expect(PhoneLinkContext(["v": 1, "enroll_code": "wce_CODE", "expires_at": 1.0, "account": 7])?.account == nil)
        #expect(with.description.contains("<redacted>"))
        #expect(!with.description.contains("wce_CODE"))
    }

    @Test func isRedacted() {
        let context = PhoneLinkContext(enrollCode: "wce_SECRET-CODE", expiresAt: 1_800_000_000)
        var dumped = ""
        dump(context, to: &dumped)
        for text in ["\(context)", String(reflecting: context), String(describing: context), dumped] {
            #expect(!text.contains("SECRET-CODE"))
            #expect(text.contains("<redacted>"))
        }
        #expect(context.dictionary["enroll_code"] as? String == "wce_SECRET-CODE")
    }
}
