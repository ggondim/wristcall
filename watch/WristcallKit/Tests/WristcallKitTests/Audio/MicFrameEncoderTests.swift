import AVFAudio
import Foundation
import Testing
import WristcallKit

struct MicFrameEncoderTests {
    /// Microphone formats the watch may report (Review Focus 5) and tap buffer sizes that do not divide 20 ms.
    @Test(arguments: [
        (48_000.0, AVAudioChannelCount(1), AVAudioFrameCount(4_800)),
        (44_100.0, 1, 4_410),
        (16_000.0, 1, 1_600),
        (48_000.0, 2, 1_024),
        (44_100.0, 2, 1_000),
        (24_000.0, 1, 512),
    ])
    func anyInputBecomes640ByteFramesOf16kHzMono(
        sampleRate: Double,
        channels: AVAudioChannelCount,
        chunk: AVAudioFrameCount
    ) throws {
        let chunks = AudioTestSignals.sineChunks(sampleRate: sampleRate, channels: channels, seconds: 1, chunk: chunk)
        let encoder = try MicFrameEncoder(inputFormat: chunks[0].format)

        var frames: [Data] = []
        for buffer in chunks {
            frames += try encoder.encode(buffer)
        }
        frames += try encoder.flush()

        #expect(frames.allSatisfy { $0.count == ProtocolConstants.frameBytes })
        // 1 s is 50 frames of 20 ms; the converter may add or hold back less than one frame.
        #expect((49...51).contains(frames.count), "got \(frames.count) frames")

        // A 0.5 amplitude sine has an RMS of about 0.35; skip the first frames (converter warm-up).
        let samples = AudioTestSignals.samples(frames.dropFirst(2).dropLast(2).reduce(Data(), +))
        let rms = AudioTestSignals.rms(samples)
        #expect(rms > 0.3 && rms < 0.4, "rms \(rms)")
    }

    @Test func stereoIsMixedNotTruncatedToTheLeftChannel() throws {
        // Voice only on the right channel: keeping only channel 0 would send silence.
        let chunks = AudioTestSignals.sineChunks(sampleRate: 48_000, channels: 2, seconds: 0.5, chunk: 960)
        for buffer in chunks {
            buffer.floatChannelData![0].update(repeating: 0, count: Int(buffer.frameLength))
        }
        let encoder = try MicFrameEncoder(inputFormat: chunks[0].format)
        var frames: [Data] = []
        for buffer in chunks {
            frames += try encoder.encode(buffer)
        }
        let rms = AudioTestSignals.rms(AudioTestSignals.samples(frames.dropFirst(2).reduce(Data(), +)))
        #expect(rms > 0.1, "rms \(rms)")
    }

    @Test func carriesTheRemainderAcrossCalls() throws {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
        let encoder = try MicFrameEncoder(inputFormat: format)
        var produced: [Data] = []
        // 100 samples per call: frames of 320 samples complete on calls 4, 7 and 10.
        for call in 0..<10 {
            let buffer = ramp(format: format, start: Int16(call * 100), count: 100)
            let frames = try encoder.encode(buffer)
            #expect(frames.count == ([3, 6, 9].contains(call) ? 1 : 0), "call \(call)")
            produced += frames
        }
        let samples = AudioTestSignals.samples(produced.reduce(Data(), +))
        #expect(samples == (0..<960).map { Int16($0) })
    }

    @Test func framesAreLittleEndianPCM16() throws {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
        let encoder = try MicFrameEncoder(inputFormat: format)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 320)!
        buffer.frameLength = 320
        for index in 0..<320 { buffer.int16ChannelData![0][index] = 0x0102 }
        let frame = try #require(try encoder.encode(buffer).first)
        #expect(Array(frame.prefix(4)) == [0x02, 0x01, 0x02, 0x01])
    }

    @Test func flushPadsTheLastFrameWithSilenceThenStartsOver() throws {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
        let encoder = try MicFrameEncoder(inputFormat: format)
        #expect(try encoder.encode(ramp(format: format, start: 1, count: 400)).count == 1)

        let flushed = try encoder.flush()
        #expect(flushed.count == 1)
        let samples = AudioTestSignals.samples(try #require(flushed.first))
        #expect(Array(samples.prefix(80)) == (321...400).map { Int16($0) })
        #expect(samples.dropFirst(80).allSatisfy { $0 == 0 })

        #expect(try encoder.flush().isEmpty)
        // Usable again after a flush.
        #expect(try encoder.encode(ramp(format: format, start: 0, count: 320)).count == 1)
    }

    @Test func resetDropsTheRemainder() throws {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
        let encoder = try MicFrameEncoder(inputFormat: format)
        #expect(try encoder.encode(ramp(format: format, start: 1, count: 300)).isEmpty)
        encoder.reset()
        let frames = try encoder.encode(ramp(format: format, start: 1_000, count: 320))
        #expect(frames.count == 1)
        #expect(AudioTestSignals.samples(frames[0]).first == 1_000)
    }

    @Test func rejectsABufferInAnotherFormat() throws {
        let encoder = try MicFrameEncoder(inputFormat: AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!)
        let other = AudioTestSignals.sine(sampleRate: 44_100, channels: 1, frames: 441)
        #expect(throws: AudioConversionError.formatMismatch) { try encoder.encode(other) }
    }

    @Test func emptyBufferGivesNoFrames() throws {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let encoder = try MicFrameEncoder(inputFormat: format)
        let empty = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 16)!
        #expect(try encoder.encode(empty).isEmpty)
    }

    @Test func outputFormatIsTheProtocolInput() {
        let format = MicFrameEncoder.outputFormat
        #expect(format.commonFormat == .pcmFormatInt16)
        #expect(format.sampleRate == Double(ProtocolConstants.inputSampleRate))
        #expect(format.channelCount == AVAudioChannelCount(ProtocolConstants.inputChannels))
        #expect(format.isInterleaved)
    }

    /// Int16 samples `start, start + 1, ...`.
    private func ramp(format: AVAudioFormat, start: Int16, count: Int) -> AVAudioPCMBuffer {
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count))!
        buffer.frameLength = AVAudioFrameCount(count)
        for index in 0..<count { buffer.int16ChannelData![0][index] = start + Int16(index) }
        return buffer
    }
}
