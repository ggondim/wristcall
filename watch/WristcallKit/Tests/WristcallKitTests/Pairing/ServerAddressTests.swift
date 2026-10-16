import Foundation
import Testing
@testable import WristcallKit

struct ServerAddressTests {
    @Test(arguments: [
        ("https://agent.example.com", "https://agent.example.com"),
        ("  https://agent.example.com/  ", "https://agent.example.com"),
        ("agent.example.com", "https://agent.example.com"),
        ("https://agent.example.com:8443/base/", "https://agent.example.com:8443/base"),
        ("http://localhost:8765", "http://localhost:8765"),
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765"),
        ("HTTP://LOCALHOST:8765", "HTTP://LOCALHOST:8765"),
    ])
    func accepts(text: String, expected: String) {
        #expect(ServerAddress.parse(text) == URL(string: expected))
    }

    @Test(arguments: [
        "",
        "   ",
        "http://agent.example.com",
        "http://192.168.0.10:8765",
        "http://127.0.0.1.example.com",
        "http://localhost@agent.example.com",
        "ws://agent.example.com",
        "https://",
        "https://agent.example.com/?x=1",
    ])
    func rejects(text: String) {
        #expect(ServerAddress.parse(text) == nil)
    }

    @Test func canonicalDropsDefaultPortAndCase() {
        #expect(ServerAddress.canonical(URL(string: "HTTPS://Example.COM:443/")!) == "https://example.com")
        #expect(ServerAddress.canonical(URL(string: "https://example.com:8443/base/")!) == "https://example.com:8443/base")
        #expect(ServerAddress.canonical(URL(string: "http://127.0.0.1:8765")!) == "http://127.0.0.1:8765")
        #expect(ServerAddress.canonical(URL(string: "http://LOCALHOST:80")!) == "http://localhost")
        #expect(ServerAddress.canonical(URL(string: "https://example.com:80")!) == "https://example.com:80")
        #expect(ServerAddress.canonical(URL(string: "https://example.com/Base/")!) == "https://example.com/Base")
    }

    @Test func idnHostIsPunycode() throws {
        let idn = try #require(URL(string: "https://Café.Example"))
        #expect(ServerAddress.canonical(idn) == "https://xn--caf-dma.example")
        #expect(ServerAddress.displayHost(idn) == "xn--caf-dma.example")
        let parsed = try #require(ServerAddress.parse("café.example"))
        #expect(ServerAddress.canonical(parsed) == "https://xn--caf-dma.example")
        #expect(ServerAddress.displayHost(try #require(URL(string: "https://home.example.com:8443"))) == "home.example.com:8443")
    }

    @Test func displayHostBracketsIPv6AndStaysASCII() throws {
        #expect(ServerAddress.displayHost(try #require(URL(string: "http://[::1]:8443"))) == "[::1]:8443")
        #expect(ServerAddress.displayHost(try #require(URL(string: "http://[::1]"))) == "[::1]")
        #expect(ServerAddress.displayHost(try #require(URL(string: "https://Home.Example.com"))) == "home.example.com")
    }
}
