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
}
