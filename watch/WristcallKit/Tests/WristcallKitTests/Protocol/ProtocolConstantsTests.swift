import Testing
import WristcallKit

struct ProtocolConstantsTests {
    @Test func matchesProtocolV1() {
        #expect(ProtocolConstants.version == 1)
        #expect(ProtocolConstants.inputSampleRate == 16_000)
        #expect(ProtocolConstants.inputChannels == 1)
        #expect(ProtocolConstants.frameMilliseconds == 20)
        #expect(ProtocolConstants.frameBytes == 640)
        #expect(ProtocolConstants.callPath == "/v1/call")
    }

    @Test func frameBytesIsTwentyMillisecondsOfMonoPCM16() {
        let samplesPerFrame = ProtocolConstants.inputSampleRate * ProtocolConstants.frameMilliseconds / 1000
        #expect(samplesPerFrame * 2 * ProtocolConstants.inputChannels == ProtocolConstants.frameBytes)
    }

    @Test func inputFormatIsPCM16At16kHzMono() {
        #expect(AudioFormat.input == AudioFormat(codec: "pcm16", sampleRate: 16_000, channels: 1))
    }

    @Test func closeCodes() {
        #expect(CloseCode.normal.rawValue == 1000)
        #expect(CloseCode.protocolError.rawValue == 4400)
        #expect(CloseCode.unauthorized.rawValue == 4401)
        #expect(CloseCode(rawValue: 4401) == .unauthorized)
        #expect(CloseCode(rawValue: 1001) == nil)
    }
}
