#if os(macOS)
import Foundation
import Synchronization
import Testing
import WristcallKit

/// A whole call against the local test server (fake providers: `fake_stt` always hears
/// "hello", `echo_chat` repeats it, `tone_tts` speaks a tone at 24 kHz).
@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run integration tests"))
struct CallIntegrationTests {
    @Test(.timeLimit(.minutes(1)))
    func speechTurnEndToEnd() async throws {
        let device = try await TestDevices.shared()
        let transport = RecordingTransport(
            try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        )
        let session = CallSession(transport: transport)

        let ready = try await session.start(profile: "demo")
        #expect(ready.profile.name == "demo")
        let audioOut = try #require(session.audioOut)
        #expect(audioOut.codec == "pcm16")
        #expect(audioOut.channels == 1)
        #expect(audioOut.sampleRate == 24_000)

        // Speech, then 1.2 s of silence (the energy VAD closes the turn after 800 ms), in
        // 640-byte frames paced at 20 ms like the microphone.
        let speech = try WAVFixture.pcm16(named: "speech_pt_16k.wav")
        let silence = Data(count: ProtocolConstants.frameBytes * 60)
        let sender = Task {
            for frame in WAVFixture.frames(of: speech + silence) {
                session.sendAudio(frame)
                try await Task.sleep(for: .milliseconds(20))
            }
        }

        var userEnd: UserTurnEndReason?
        var transcripts: [CallEvent] = []
        var agentStarted = false
        var agentBytes = 0
        for await event in session.events {
            switch event {
            case .userTurnEnded(let reason):
                if userEnd == nil { userEnd = reason }
            case .transcript:
                transcripts.append(event)
            case .agentTurnStarted:
                agentStarted = true
            case .agentAudio(let data):
                #expect(agentStarted, "agent audio before turn.agent_start")
                agentBytes += data.count
            case .agentTurnEnded:
                sender.cancel()
                await session.end()
            case .error(let code, let message, _):
                Issue.record("server error \(code.wireValue): \(message)")
            case .ended(let reason):
                #expect(reason == .normal)
            }
        }

        #expect(userEnd == .vad)
        #expect(transcripts == [
            .transcript(role: .user, text: "hello"),
            .transcript(role: .assistant, text: "You said: hello"),
        ])
        #expect(agentBytes > 0)
        #expect(agentBytes % 2 == 0)
        #expect(transport.closeCodes == [CloseCode.normal.rawValue])
        #expect(transport.sentTexts.last == (try ClientMessage.sessionEnd.jsonText()))
        #expect(transport.sentBinaryBytes >= speech.count)
    }

    @Test func unknownProfileEndsWithServerFatal() async throws {
        let device = try await TestDevices.shared()
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        let session = CallSession(transport: transport)
        await #expect(throws: CallSessionError.ended(.serverFatal(.unknownProfile))) {
            try await session.start(profile: "no-such-profile")
        }
        var events: [CallEvent] = []
        for await event in session.events {
            events.append(event)
        }
        #expect(events.last == .ended(.serverFatal(.unknownProfile)))
    }

    @Test func invalidTokenEndsAsUnauthorized() async throws {
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: "not-a-real-token")
        let session = CallSession(transport: transport)
        await #expect(throws: CallSessionError.ended(.unauthorized)) {
            try await session.start(profile: nil)
        }
    }
}

/// Wraps a transport and records what the session sends and how it closes.
final class RecordingTransport: CallTransport {
    let base: any CallTransport
    private let log = Mutex<(texts: [String], binaryBytes: Int, closes: [UInt16])>(([], 0, []))

    init(_ base: any CallTransport) {
        self.base = base
    }

    var events: AsyncStream<TransportEvent> { base.events }
    var sentTexts: [String] { log.withLock { $0.texts } }
    var sentBinaryBytes: Int { log.withLock { $0.binaryBytes } }
    var closeCodes: [UInt16] { log.withLock { $0.closes } }

    func connect() async throws {
        try await base.connect()
    }

    func send(text: String) async throws {
        try await base.send(text: text)
        log.withLock { $0.texts.append(text) }
    }

    func send(binary: Data) async throws {
        try await base.send(binary: binary)
        log.withLock { $0.binaryBytes += binary.count }
    }

    func close(code: UInt16) async {
        log.withLock { $0.closes.append(code) }
        await base.close(code: code)
    }
}

/// PCM16 fixtures from `server/tests/fixtures`.
enum WAVFixture {
    static let directory = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()  // WristcallKitIntegrationTests
        .deletingLastPathComponent()  // Tests
        .deletingLastPathComponent()  // WristcallKit
        .deletingLastPathComponent()  // watch
        .deletingLastPathComponent()  // repo root
        .appending(path: "server/tests/fixtures")

    struct FormatError: Error, CustomStringConvertible {
        let description: String
    }

    /// The `data` chunk of a PCM16 LE mono 16 kHz WAV file.
    static func pcm16(named name: String) throws -> Data {
        let file = try Data(contentsOf: directory.appending(path: name))
        guard file.count >= 12, file.prefix(4) == Data("RIFF".utf8), file[8..<12] == Data("WAVE".utf8) else {
            throw FormatError(description: "\(name) is not a RIFF/WAVE file")
        }
        var offset = 12
        var format: (tag: UInt16, channels: UInt16, rate: UInt32, bits: UInt16)?
        while offset + 8 <= file.count {
            let id = String(decoding: file[offset..<offset + 4], as: UTF8.self)
            let size = Int(littleEndian32(file, at: offset + 4))
            let body = offset + 8
            guard body + size <= file.count else { break }
            if id == "fmt " {
                format = (
                    UInt16(littleEndian32(file, at: body) & 0xFFFF),
                    UInt16(littleEndian32(file, at: body) >> 16),
                    littleEndian32(file, at: body + 4),
                    UInt16(littleEndian32(file, at: body + 12) >> 16)
                )
            } else if id == "data" {
                guard let format, format.tag == 1, format.channels == 1, format.rate == 16_000, format.bits == 16 else {
                    throw FormatError(description: "\(name) is not PCM16 mono 16 kHz: \(String(describing: format))")
                }
                return Data(file[body..<body + size])
            }
            offset = body + size + (size & 1)
        }
        throw FormatError(description: "\(name) has no data chunk")
    }

    /// 640-byte frames; the last one padded with silence.
    static func frames(of pcm: Data) -> [Data] {
        let size = ProtocolConstants.frameBytes
        return stride(from: 0, to: pcm.count, by: size).map { start in
            var frame = Data(pcm[pcm.startIndex + start..<pcm.startIndex + min(start + size, pcm.count)])
            if frame.count < size {
                frame.append(Data(count: size - frame.count))
            }
            return frame
        }
    }

    private static func littleEndian32(_ data: Data, at offset: Int) -> UInt32 {
        data[data.startIndex + offset..<data.startIndex + offset + 4]
            .enumerated()
            .reduce(UInt32(0)) { $0 | UInt32($1.element) << (8 * UInt32($1.offset)) }
    }
}
#endif
