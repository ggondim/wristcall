import Foundation
import Testing
import WristcallKit

struct WristcallLinkTests {
    private static let code = "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"

    private func parse(_ text: String) -> WristcallLink? {
        guard let url = URL(string: text) else { return nil }
        return WristcallLink.parse(url)
    }

    /// For negative cases: a URL that fails to build must fail the test, not pass as "rejected".
    private func rejects(_ text: String, sourceLocation: SourceLocation = #_sourceLocation) throws {
        let url = try #require(URL(string: text), sourceLocation: sourceLocation)
        #expect(WristcallLink.parse(url) == nil, sourceLocation: sourceLocation)
    }

    private func server(_ url: String) -> WristcallLink {
        .server(url: URL(string: url)!, code: LinkCode(Self.code)!)
    }

    // MARK: - Server links

    @Test(arguments: [
        "https://wristcall-app.trigram.com.br/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/link#v=1&s=https%3A%2F%2Fhome.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/link#c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ&s=https://home.example.com&v=1",
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
    ])
    func parsesServerLinks(text: String) {
        #expect(parse(text) == server("https://home.example.com"))
    }

    @Test(arguments: [
        "https://wristcall-app.trigram.com.br/link#v=1&s=https%3A%2F%2Fhome.example.com%3A8443%2Fwc&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https%3A%2F%2Fhome.example.com%3A8443%2Fwc&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com:8443/wc&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
    ])
    func parsesServerLinksWithPortAndPath(text: String) {
        #expect(parse(text) == server("https://home.example.com:8443/wc"))
    }

    @Test func acceptsATypedCodeSpelling() {
        let text = "wristcall://link#v=1&s=https://home.example.com&c=k7q2-mzxw4rqt6pzj3h5n2c7vbq"
        #expect(parse(text) == server("https://home.example.com"))
    }

    @Test func plusIsALiteralPlus() {
        let text = "wristcall://link#v=1&s=https://home.example.com/a+b&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
        guard case .server(let url, _)? = parse(text) else {
            Issue.record("expected a server link")
            return
        }
        #expect(url.path == "/a+b")
        #expect(url == URL(string: "https://home.example.com/a+b"))
    }

    @Test func schemeAndHostAreCaseInsensitive() {
        let text = "WristCall://LINK#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
        #expect(parse(text) == server("https://home.example.com"))
        let web = "HTTPS://WRISTCALL-APP.TRIGRAM.COM.BR/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
        #expect(parse(web) == server("https://home.example.com"))
    }

    @Test(arguments: [
        // invalid percent escapes must not crash
        "wristcall://link#v=1&s=https://home.example.com&c=%",
        "wristcall://link#v=1&s=%ZZ&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com%&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VB%",
        // space in s
        "wristcall://link#v=1&s=https://home example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home%20example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // version
        "wristcall://link#v=2&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // missing or bad code
        "wristcall://link#v=1&s=https://home.example.com",
        "wristcall://link#v=1&s=https://home.example.com&c=",
        "wristcall://link#v=1&s=https://home.example.com&c=SHORT",
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VB1",
        // plus is a literal plus, never a space
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7V+Q",
        // s: query, fragment, http, loopback, empty, missing, other schemes
        "wristcall://link#v=1&s=https://home.example.com?x=1&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https%3A%2F%2Fhome.example.com%3Fx%3D1&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https%3A%2F%2Fhome.example.com%23frag&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=http://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=http://localhost:8080&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=http://127.0.0.1:8080&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=ftp://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // well formed escapes that are not valid UTF-8 reach the decode failure
        "wristcall://link#v=1&s=https://home.example.com&c=%C3%28",
        "wristcall://link#v=1&s=%FF&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com/%FF&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // empty segments: a trailing, doubled or leading ampersand
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ&",
        "wristcall://link#v=1&&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#&v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // duplicates and malformed pairs
        "wristcall://link#v=1&v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com&s=https://other.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ&novalue",
        "wristcall://link#",
        "wristcall://link",
    ])
    func rejectsBadServerLinks(text: String) throws {
        try rejects(text)
    }

    @Test(arguments: [
        // wrong host or scheme
        "https://evil.example/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "http://wristcall-app.trigram.com.br/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br.evil.example/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://evil.example@wristcall-app.trigram.com.br/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br:8443/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://other#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "other://link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // wrong path
        "https://wristcall-app.trigram.com.br/other#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/link/extra#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link/extra#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // only the strict forms: host form for the scheme, no port, userinfo, trailing slash or trailing dot
        "wristcall:/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall:///link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall:link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link/#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link.#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link:443#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://user@link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/link/#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br:443/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br./link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://user@wristcall-app.trigram.com.br/link#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        // parameters in the query instead of the fragment
        "https://wristcall-app.trigram.com.br/link?v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "wristcall://link?v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
        "https://wristcall-app.trigram.com.br/link?x=1#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ",
    ])
    func rejectsWrongPlaces(text: String) throws {
        try rejects(text)
    }

    // MARK: - Web session links

    @Test(arguments: [
        "https://wristcall-app.trigram.com.br/approve#w=ws_0123456789abcdef&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef&k=xyz",
        "https://wristcall-app.trigram.com.br/approve#k=xyz&w=ws_0123456789abcdef",
    ])
    func parsesWebSessionLinks(text: String) {
        #expect(parse(text) == .webSession(id: "ws_0123456789abcdef", key: "xyz"))
    }

    @Test func webSessionKeyBounds() {
        let longest = String(repeating: "aZ_-", count: 32)
        #expect(longest.count == 128)
        #expect(parse("wristcall://approve#w=ws_0123456789abcdef&k=\(longest)")
            == .webSession(id: "ws_0123456789abcdef", key: longest))
        #expect(parse("wristcall://approve#w=ws_0123456789abcdef&k=\(longest)a") == nil)
        #expect(parse("wristcall://approve#w=ws_0123456789abcdef&k=") == nil)
    }

    @Test(arguments: [
        "wristcall://approve#w=ws_0123456789abcde&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef0&k=xyz",
        "wristcall://approve#w=ws_0123456789ABCDEF&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdeg&k=xyz",
        "wristcall://approve#w=0123456789abcdef&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef&k=x.z",
        "wristcall://approve#w=ws_0123456789abcdef&k=x+z",
        "wristcall://approve#w=ws_0123456789abcdef&k=x%20z",
        "wristcall://approve#w=ws_0123456789abcdef&k=%",
        "wristcall://approve#w=%&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef&k=%FF",
        "wristcall://approve#w=ws_0123456789abcdef&k=xyz&",
        "wristcall:/approve#w=ws_0123456789abcdef&k=xyz",
        "wristcall:///approve#w=ws_0123456789abcdef&k=xyz",
        "wristcall://approve/#w=ws_0123456789abcdef&k=xyz",
        "https://wristcall-app.trigram.com.br/approve/#w=ws_0123456789abcdef&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef",
        "wristcall://approve#k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef&w=ws_0123456789abcdef&k=xyz",
        "wristcall://approve#w=ws_0123456789abcdef&k=xyz&k=xyz",
        "wristcall://approve?w=ws_0123456789abcdef&k=xyz",
        "https://evil.example/approve#w=ws_0123456789abcdef&k=xyz",
        "http://wristcall-app.trigram.com.br/approve#w=ws_0123456789abcdef&k=xyz",
        "https://wristcall-app.trigram.com.br/approve/x#w=ws_0123456789abcdef&k=xyz",
    ])
    func rejectsBadWebSessionLinks(text: String) throws {
        try rejects(text)
    }

    @Test func percentEncodedKeyDecodesBeforeValidation() {
        #expect(parse("wristcall://approve#w=ws_0123456789abcdef&k=%41bc")
            == .webSession(id: "ws_0123456789abcdef", key: "Abc"))
    }

    @Test func constants() {
        #expect(WristcallLink.webHost == "wristcall-app.trigram.com.br")
        #expect(WristcallLink.scheme == "wristcall")
    }
}
