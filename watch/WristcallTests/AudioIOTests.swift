import AVFAudio
import Foundation
import Testing
import WristcallKit
@testable import Wristcall

struct MicGateTests {
    /// `seconds` of a 440 Hz tone at 48 kHz Float32 mono, the format the watch reports most often.
    static func tone(seconds: Double, rate: Double = 48_000) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
        let frames = AVAudioFrameCount(seconds * rate)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for i in 0..<Int(frames) {
            buffer.floatChannelData![0][i] = 0.3 * sin(2 * .pi * 440 * Float(i) / Float(rate))
        }
        return buffer
    }

    static func makeGate(muted: Bool = false) throws -> (MicGate, Recorder<Data>) {
        let frames = Recorder<Data>()
        let encoder = try MicFrameEncoder(inputFormat: tone(seconds: 0.01).format)
        return (MicGate(encoder: encoder, muted: muted) { frames.append($0) }, frames)
    }

    @Test func deliversWholeProtocolFrames() throws {
        let (gate, frames) = try Self.makeGate()

        // 1024-frame buffers, like the tap asks for.
        for _ in 0..<47 {
            gate.process(Self.tone(seconds: 1024.0 / 48_000))
        }

        // 47 × 1024 frames at 48 kHz ≈ 1.003 s ≈ 50 frames of 20 ms; the converter keeps up to one.
        #expect((49...50).contains(frames.all.count))
        #expect(frames.all.allSatisfy { $0.count == ProtocolConstants.frameBytes })
    }

    @Test func mutedGateDeliversNothing() throws {
        let (gate, frames) = try Self.makeGate(muted: true)

        gate.process(Self.tone(seconds: 0.5))

        #expect(frames.all.isEmpty)
        #expect(gate.isMuted)
    }

    @Test func muteFlushesTheSpeechInTheEncoderFirst() throws {
        let (gate, frames) = try Self.makeGate()
        gate.process(Self.tone(seconds: 0.03))  // 1 frame out, ~10 ms waiting in the encoder
        let beforeMute = frames.all.count

        gate.setMuted(true)
        let afterMute = frames.all.count
        gate.process(Self.tone(seconds: 0.5))

        #expect(afterMute > beforeMute)
        #expect(frames.all.count == afterMute)
        #expect(frames.all.allSatisfy { $0.count == ProtocolConstants.frameBytes })
    }

    @Test func unmuteDropsLeftoversAndResumes() throws {
        let (gate, frames) = try Self.makeGate()
        gate.setMuted(true)
        gate.setMuted(true)  // repeated: nothing to flush
        #expect(frames.all.isEmpty)

        gate.setMuted(false)
        gate.process(Self.tone(seconds: 0.2))

        #expect((9...10).contains(frames.all.count))
    }

    @Test func bufferInAnotherFormatIsDropped() throws {
        let (gate, frames) = try Self.makeGate()

        gate.process(Self.tone(seconds: 0.5, rate: 44_100))

        #expect(frames.all.isEmpty)
    }
}

struct PlaybackFeedTests {
    let output = AVAudioFormat(standardFormatWithSampleRate: 24_000, channels: 1)!

    @Test func schedulesDecodedAudioAndSkipsEmptyBuffers() throws {
        let scheduled = Recorder<AVAudioFrameCount>()
        let feed = PlaybackFeed(decoder: try PlaybackDecoder(sampleRate: 24_000, outputFormat: output)) {
            scheduled.append($0.frameLength)
        }

        feed.play(Data())                       // nothing
        feed.play(Data([0x01]))                 // odd byte, kept for the next frame
        feed.play(Data(count: 959))             // + carried byte = 480 samples (20 ms)
        feed.turnEnded()

        #expect(scheduled.all.count >= 1)
        #expect(scheduled.all.allSatisfy { $0 > 0 })
        #expect(scheduled.all.reduce(0, +) == 480)
    }
}

@MainActor
struct AudioIOTests {
    @Test func prepareSetsPlayAndRecordVoiceChat() throws {
        let session = AVAudioSession.sharedInstance()

        try AudioIO(session: session).prepare()

        #expect(session.category == .playAndRecord)
        #expect(session.mode == .voiceChat)
    }

    @Test func rejectsAPlaybackRateOfZero() {
        let audio = AudioIO()

        #expect(throws: AudioIOError.unsupportedPlaybackRate(0)) {
            try audio.start(playbackSampleRate: 0) { _ in }
        }
        #expect(!audio.isRunning)
    }

    @Test func tapBufferIsAtMost1024Frames() {
        #expect(AudioIO.tapBufferSize <= 1024)
    }

    @Test func stopBeforeStartIsHarmless() {
        let audio = AudioIO()
        audio.stop()
        audio.setMuted(true)
        audio.play(Data(count: 960))
        audio.agentTurnEnded()
        audio.stop()
        #expect(!audio.isRunning)
    }
}

#if DEBUG
struct SyntheticMicTests {
    @Test func toneForOneSecondThenTwoSecondsOfSilence() {
        func peak(_ tick: Int) -> Float {
            let buffer = SyntheticMic.buffer(tick: tick)
            return withExtendedLifetime(buffer) {
                UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength)).map(abs).max() ?? 0
            }
        }

        #expect(SyntheticMic.buffer(tick: 0).frameLength == 960)
        #expect(SyntheticMic.buffer(tick: 0).format == SyntheticMic.format)
        #expect(peak(0) > 0.25)
        #expect(peak(SyntheticMic.toneTicks - 1) > 0.25)
        #expect(peak(SyntheticMic.toneTicks) == 0)
        #expect(peak(SyntheticMic.cycleTicks - 1) == 0)
        #expect(peak(SyntheticMic.cycleTicks) > 0.25)
    }
}
#endif
