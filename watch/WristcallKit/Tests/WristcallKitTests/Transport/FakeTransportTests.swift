import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

/// The double must behave like the real transport, or the CallSession tests prove nothing.
struct FakeTransportTests {
    @Test func recordsFramesAndServerEvents() async throws {
        let transport = FakeTransport()
        await #expect(throws: TransportError.notConnected) {
            try await transport.send(text: "early")
        }
        try await transport.connect()
        try await transport.send(text: "hello")
        try await transport.send(binary: Data([1, 2]))
        transport.serverSends("{\"type\":\"turn.agent_start\"}")
        transport.serverSends(binary: Data([3]))
        await transport.close(code: 1000)
        await transport.close(code: 4000)

        #expect(transport.sent == [.text("hello"), .binary(Data([1, 2])), .close(1000)])
        var events: [TransportEvent] = []
        for await event in transport.events {
            events.append(event)
        }
        #expect(events == [
            .text("{\"type\":\"turn.agent_start\"}"),
            .binary(Data([3])),
            .closed(code: 1000),
        ])
        await #expect(throws: TransportError.notConnected) {
            try await transport.send(text: "late")
        }
    }

    @Test func serverCloseEndsTheStream() async throws {
        let transport = FakeTransport()
        try await transport.connect()
        transport.serverCloses(code: 4401)
        transport.serverSends("ignored")
        var events: [TransportEvent] = []
        for await event in transport.events {
            events.append(event)
        }
        #expect(events == [.closed(code: 4401)])
        await transport.close(code: 1000)
        #expect(transport.sent.isEmpty)
    }

    @Test func connectErrorEndsTheStreamWithoutCode() async throws {
        let transport = FakeTransport(connectError: .connectionFailed("refused"))
        await #expect(throws: TransportError.connectionFailed("refused")) {
            try await transport.connect()
        }
        var events: [TransportEvent] = []
        for await event in transport.events {
            events.append(event)
        }
        #expect(events == [.closed(code: nil)])
    }
}
