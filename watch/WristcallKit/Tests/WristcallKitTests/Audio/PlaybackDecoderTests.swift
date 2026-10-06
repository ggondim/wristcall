import AVFAudio
import Foundation
import Testing
import WristcallKit

struct PlaybackDecoderTests {
    /// Server rates (24 kHz from `tone_tts`, 16 kHz) into output formats a watch mixer may use.
    @Test(arguments: [
        (24_000, 48_000.0, AVAudioChannelCount(1)),
        (24_000, 44_100.0, 2),
        (16_000, 48_000.0, 1),
        (24_000, 24_000.0, 1),
        (22_050, 48_000.0, 2),
    ])
    func outputLengthFollowsTheRateRatio(inputRate: Int, outputRate: Double, channels: AVAudioChannelCount) throws {
        let output = AVAudioFormat(standardFormatWithSampleRate: outputRate, channels: channels)!
        let decoder = try PlaybackDecoder(sampleRate: inputRate, outputFormat: output)
        let pcm = AudioTestSignals.pcm16Sine(sampleRate: inputRate, seconds: 1)

        // Odd chunk sizes, as WebSocket frames may split samples.
        var buffers: [AVAudioPCMBuffer] = []
        var offset = 0
        while offset < pcm.count {
            let end = min(offset + 1_999, pcm.count)
            buffers.append(try decoder.decode(pcm.subdata(in: offset..<end)))
            offset = end
        }
        buffers.append(try decoder.flush())

        #expect(buffers.allSatisfy { $0.format == output })
        let total = buffers.reduce(0) { $0 + Int($1.frameLength) }
        let expected = Int(outputRate)  // 1 s
        #expect(abs(total - expected) <= Int(outputRate / 100), "got \(total) frames, expected about \(expected)")

        // Every channel carries the sine (mono is copied to both sides, not left only).
        let middle = try #require(buffers.dropFirst(buffers.count / 2).first { $0.frameLength > 0 })
        for channel in 0..<Int(channels) {
            let rms = AudioTestSignals.rms(middle, channel: channel)
            #expect(rms > 0.3 && rms < 0.4, "channel \(channel) rms \(rms)")
        }
    }

    @Test func carriesAnOddByteToTheNextCall() throws {
        let output = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
        let decoder = try PlaybackDecoder(sampleRate: 16_000, outputFormat: output)
        // 0x4000 = 16384 = 0.5; split as [00 40 00] + [40].
        let first = try decoder.decode(Data([0x00, 0x40, 0x00]))
        #expect(first.frameLength == 1)
        #expect(first.floatChannelData![0][0] == 0.5)
        let second = try decoder.decode(Data([0x40]))
        #expect(second.frameLength == 1)
        #expect(second.floatChannelData![0][0] == 0.5)
    }

    @Test func singleByteGivesAnEmptyBuffer() throws {
        let output = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let decoder = try PlaybackDecoder(sampleRate: 24_000, outputFormat: output)
        let buffer = try decoder.decode(Data([0x01]))
        #expect(buffer.frameLength == 0)
        #expect(buffer.format == output)
        #expect(try decoder.decode(Data()).frameLength == 0)
    }

    @Test func resetDropsTheCarriedByte() throws {
        let output = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1)!
        let decoder = try PlaybackDecoder(sampleRate: 16_000, outputFormat: output)
        _ = try decoder.decode(Data([0x00, 0x40, 0xFF]))
        decoder.reset()
        let buffer = try decoder.decode(Data([0x00, 0x40]))
        #expect(buffer.frameLength == 1)
        #expect(buffer.floatChannelData![0][0] == 0.5)
    }

    @Test func rejectsANonPositiveRate() {
        let output = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        #expect(throws: AudioConversionError.unsupportedFormat) { try PlaybackDecoder(sampleRate: 0, outputFormat: output) }
    }
}
