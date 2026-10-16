import Testing
import WristcallKit

struct LinkCodeTests {
    private static let sample = "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"

    @Test(arguments: [
        "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "k7q2mzxw4rqt6pzj3h5n2c7vbq",
        "K7Q2-MZXW 4RQT-6PZJ 3H5N-2C7V BQ",
        " K7Q2MZXW4RQT6PZJ3H5N2C7VBQ\n",
    ])
    func normalizes(raw: String) throws {
        let code = try #require(LinkCode(raw))
        #expect(code.value == Self.sample)
        #expect(LinkCode.normalize(raw) == Self.sample)
    }

    @Test(arguments: [
        "",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VBQA",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB0",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB1",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB8",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB9",
        "K7Q2MZXW4RQT6PZJ3H5N2C7VB=",
        "K7Q2MZXW4RQT6PZJ3H5N2C7V٣",
        "K7Q2MZXW4RQT6PZJ3H5N2C7V３",
        "K7Q2MZXW4RQT6PZJ3H5N2C7V_",
    ])
    func rejectsAnythingElse(raw: String) {
        #expect(LinkCode(raw) == nil)
        #expect(LinkCode.normalize(raw) == nil)
    }

    @Test func printingNeverShowsTheCode() throws {
        let code = try #require(LinkCode(Self.sample))
        var dumped = ""
        dump(code, to: &dumped)
        dump([code], to: &dumped)
        let texts = [code.description, code.debugDescription, "\(code)", String(reflecting: code), dumped,
                     "\(Optional(code))", "\([code])"]
        for text in texts {
            #expect(!text.contains(Self.sample))
            #expect(!text.contains("K7Q2"))
            #expect(!text.contains("VBQ"))
            #expect(text.contains("<redacted>"))
        }
        #expect(code.value == Self.sample)
    }
}
