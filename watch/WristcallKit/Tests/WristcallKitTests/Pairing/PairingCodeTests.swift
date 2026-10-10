import Testing
import WristcallKit

struct PairingCodeTests {
    @Test(arguments: ["12345678", "1234 5678", "1234-5678", " 12 34-56 78 ", "1234\t5678"])
    func acceptsEightDigitsIgnoringSpacesAndHyphens(raw: String) throws {
        let code = try #require(PairingCode(raw))
        #expect(code.digits == "12345678")
        #expect(PairingCode.normalize(raw) == "12345678")
    }

    @Test(arguments: ["", "1234567", "123456789", "1234 567a", "1234_5678", "1234.5678", "١٢٣٤٥٦٧٨", "１２３４５６７８"])
    func rejectsAnythingElse(raw: String) {
        #expect(PairingCode(raw) == nil)
        #expect(PairingCode.normalize(raw) == nil)
    }

    @Test func formatsAsTwoGroupsOfFour() throws {
        let code = try #require(PairingCode("12-34-56-78"))
        #expect(code.formatted == "1234 5678")
    }

    @Test func printingNeverShowsTheDigits() throws {
        let code = try #require(PairingCode("12345678"))
        var dumped = ""
        dump(code, to: &dumped)
        dump([code], to: &dumped)
        let texts = [code.description, code.debugDescription, "\(code)", String(reflecting: code), dumped,
                     "\(Optional(code))", "\([code])"]
        for text in texts {
            #expect(!text.contains("12345678"))
            #expect(!text.contains("1234"))
            #expect(!text.contains("5678"))
            #expect(text.contains("<redacted>"))
        }
        // The value itself is intact.
        #expect(code.digits == "12345678")
        #expect(code.formatted == "1234 5678")
    }
}
