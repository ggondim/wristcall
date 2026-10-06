import Foundation
import Testing
@testable import Wristcall

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
}
