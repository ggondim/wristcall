import Foundation
import Network
import Testing
@testable import WristcallKit

struct NWWebSocketTransportTests {
    @Test(arguments: [
        ("https://agent.example.com", "wss://agent.example.com/v1/call"),
        ("https://agent.example.com/", "wss://agent.example.com/v1/call"),
        ("https://example.com/wristcall/", "wss://example.com/wristcall/v1/call"),
        ("http://127.0.0.1:8765", "ws://127.0.0.1:8765/v1/call"),
        ("HTTPS://Agent.example.com?x=1#y", "wss://Agent.example.com/v1/call"),
        ("wss://agent.example.com", "wss://agent.example.com/v1/call"),
        ("ws://localhost:8765", "ws://localhost:8765/v1/call"),
    ])
    func webSocketURLFromServerURL(server: String, expected: String) throws {
        let url = try NWWebSocketTransport.webSocketURL(server: try #require(URL(string: server)))
        #expect(url.absoluteString == expected)
    }

    @Test(arguments: ["ftp://agent.example.com", "file:///tmp/x", "agent.example.com"])
    func rejectsOtherSchemes(server: String) throws {
        let url = try #require(URL(string: server))
        #expect(throws: TransportError.unsupportedURL(server)) {
            try NWWebSocketTransport.webSocketURL(server: url)
        }
    }

    @Test(arguments: [UInt16(1000), 1001, 1011, 3000, 4400, 4401])
    func closeCodesRoundTrip(raw: UInt16) {
        #expect(NWWebSocketTransport.rawCode(NWWebSocketTransport.closeCode(raw)) == raw)
    }

    @Test func applicationCodesAreSentAsPrivateCodes() {
        #expect(NWWebSocketTransport.closeCode(4401) == .privateCode(4401))
        #expect(NWWebSocketTransport.closeCode(1000) == .protocolCode(.normalClosure))
    }

    @Test func sendBeforeConnectThrowsNotConnected() async throws {
        let transport = NWWebSocketTransport(webSocketURL: URL(string: "ws://127.0.0.1:9/v1/call")!, token: "x")
        await #expect(throws: TransportError.notConnected) {
            try await transport.send(text: "{}")
        }
    }

    @Test func closeBeforeConnectEndsTheStreamOnce() async throws {
        let transport = NWWebSocketTransport(webSocketURL: URL(string: "ws://127.0.0.1:9/v1/call")!, token: "x")
        await transport.close(code: 1000)
        await transport.close(code: 4000)
        var events: [TransportEvent] = []
        for await event in transport.events {
            events.append(event)
        }
        #expect(events == [.closed(code: 1000)])
        await #expect(throws: TransportError.notConnected) {
            try await transport.connect()
        }
    }

    /// After a successful connect, `close(code:)` ends the stream exactly once with its own code,
    /// although the connection's `.cancelled` state and the failing receive race the caller.
    /// Repeated, because a race shows up only some of the time.
    @Test func closeAfterConnectEndsTheStreamWithItsOwnCode() async throws {
        let server = try LoopbackWebSocketServer()
        let url = try await server.start()
        defer { server.stop() }
        for _ in 0..<25 {
            let transport = NWWebSocketTransport(webSocketURL: url, token: "t")
            try await transport.connect()
            await transport.close(code: 4000)
            var events: [TransportEvent] = []
            for await event in transport.events {
                events.append(event)
            }
            #expect(events == [.closed(code: 4000)])
            await #expect(throws: TransportError.notConnected) {
                try await transport.send(text: "late")
            }
        }
    }
}
