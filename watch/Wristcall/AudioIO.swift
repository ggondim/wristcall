import AVFAudio
import Foundation
import os
import Synchronization
import WristcallKit

/// The audio side of a call as the coordinator sees it: `AudioIO` in the app, a fake in the tests.
@MainActor
protocol CallAudio: AnyObject {
    /// Sets the session category before the call; CallKit activates the session later.
    func prepare() throws
    /// Starts the microphone and the player. Call after the session is active and
    /// `session.ready` gave the agent's sample rate. `onFrame` receives 640-byte frames
    /// (PCM16 mono 16 kHz) on the audio thread. Throws `AudioIOError` or an engine error.
    func start(playbackSampleRate: Int, onFrame: @escaping @Sendable (Data) -> Void) throws
    /// Mute delivers the speech still in the encoder, then stops delivering frames.
    /// Kept across `start`, so it may be called before it.
    func setMuted(_ muted: Bool)
    /// Queues one frame of agent audio (PCM16 mono at the playback rate).
    func play(_ data: Data)
    /// The agent's turn ended: plays what the resampler still holds.
    func agentTurnEnded()
    /// Stops the microphone and the player. Safe to call at any time, more than once.
    func stop()
}

enum AudioIOError: Error, Equatable {
    /// The input node has no usable format (the watch simulator reports 0 Hz).
    case microphoneUnavailable
    /// `session.ready` announced a sample rate the player cannot use.
    case unsupportedPlaybackRate(Int)
}

/// Between the microphone tap (audio thread) and the call: turns buffers into protocol frames
/// and delivers them unless muted.
final class MicGate: Sendable {
    private let encoder: MicFrameEncoder
    private let onFrame: @Sendable (Data) -> Void
    private let muted: Mutex<Bool>

    init(encoder: MicFrameEncoder, muted: Bool = false, onFrame: @escaping @Sendable (Data) -> Void) {
        self.encoder = encoder
        self.onFrame = onFrame
        self.muted = Mutex(muted)
    }

    var isMuted: Bool { muted.withLock { $0 } }

    /// One microphone buffer. Buffers in another format than the encoder's are dropped.
    func process(_ buffer: AVAudioPCMBuffer) {
        guard !isMuted, let frames = try? encoder.encode(buffer) else { return }
        frames.forEach(onFrame)
    }

    /// Mute: the speech still in the encoder goes out first (padded to a whole frame), then
    /// nothing until unmute. Unmute: drops whatever was left, so stale audio is never sent.
    func setMuted(_ value: Bool) {
        let changed = muted.withLock { muted -> Bool in
            defer { muted = value }
            return muted != value
        }
        guard changed else { return }
        if value {
            (try? encoder.flush())?.forEach(onFrame)
        } else {
            encoder.reset()
        }
    }
}

/// Turns the server's audio into player buffers. Empty buffers (an odd byte waiting for its
/// pair, nothing left in the resampler) are not scheduled.
struct PlaybackFeed {
    let decoder: PlaybackDecoder
    let schedule: (AVAudioPCMBuffer) -> Void

    func play(_ data: Data) {
        guard let buffer = try? decoder.decode(data) else { return }
        scheduleIfNotEmpty(buffer)
    }

    func turnEnded() {
        guard let buffer = try? decoder.flush() else { return }
        scheduleIfNotEmpty(buffer)
    }

    private func scheduleIfNotEmpty(_ buffer: AVAudioPCMBuffer) {
        guard buffer.frameLength > 0 else { return }
        schedule(buffer)
    }
}

/// Microphone and speaker of a call: `AVAudioSession` (`.playAndRecord` + `.voiceChat`),
/// an `AVAudioEngine` input tap feeding `MicFrameEncoder`, and an `AVAudioPlayerNode` fed by
/// `PlaybackDecoder` at the rate of `session.ready.audio_out`.
@MainActor
final class AudioIO: CallAudio {
    /// Requested tap size, in frames of the input format (about 21 ms at 48 kHz).
    static let tapBufferSize: AVAudioFrameCount = 1024
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "audio")

    private let session: AVAudioSession
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var gate: MicGate?
    private var feed: PlaybackFeed?
    private var tapInstalled = false
    private var muted = false

    #if DEBUG
    /// Simulator only (`-syntheticMic`): a generated tone replaces the microphone.
    var useSyntheticMic = false
    private var syntheticMic: SyntheticMic?
    #endif

    init(session: AVAudioSession = .sharedInstance()) {
        self.session = session
    }

    var isRunning: Bool { engine?.isRunning ?? false }

    func prepare() throws {
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: [])
    }

    func start(playbackSampleRate rate: Int, onFrame: @escaping @Sendable (Data) -> Void) throws {
        stop()
        guard rate > 0, let playbackFormat = AVAudioFormat(standardFormatWithSampleRate: Double(rate), channels: 1) else {
            throw AudioIOError.unsupportedPlaybackRate(rate)
        }
        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
        let decoder = try PlaybackDecoder(sampleRate: rate, outputFormat: playbackFormat)

        let gate: MicGate
        #if DEBUG
        if useSyntheticMic {
            gate = MicGate(encoder: try MicFrameEncoder(inputFormat: SyntheticMic.format), muted: muted, onFrame: onFrame)
        } else {
            gate = try installTap(on: engine, muted: muted, onFrame: onFrame)
        }
        #else
        gate = try installTap(on: engine, muted: muted, onFrame: onFrame)
        #endif

        do {
            engine.prepare()
            try engine.start()
        } catch {
            if tapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                tapInstalled = false
            }
            throw error
        }
        player.play()
        self.engine = engine
        self.player = player
        self.gate = gate
        feed = PlaybackFeed(decoder: decoder) { [player] buffer in
            player.scheduleBuffer(buffer, completionHandler: nil)
        }
        #if DEBUG
        if useSyntheticMic {
            syntheticMic = SyntheticMic(gate: gate)
        }
        #endif
        Self.log.notice("audio started: playback \(rate) Hz")
    }

    func setMuted(_ muted: Bool) {
        self.muted = muted
        gate?.setMuted(muted)
    }

    func play(_ data: Data) {
        feed?.play(data)
    }

    func agentTurnEnded() {
        feed?.turnEnded()
    }

    func stop() {
        #if DEBUG
        syntheticMic?.stop()
        syntheticMic = nil
        #endif
        if tapInstalled, let engine {
            engine.inputNode.removeTap(onBus: 0)
        }
        tapInstalled = false
        player?.stop()
        engine?.stop()
        if engine != nil {
            Self.log.notice("audio stopped")
        }
        engine = nil
        player = nil
        gate = nil
        feed = nil
    }

    private func installTap(on engine: AVAudioEngine, muted: Bool, onFrame: @escaping @Sendable (Data) -> Void) throws -> MicGate {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        // Installing a tap with a 0 Hz format raises an Objective-C exception (seen in the simulator).
        guard format.sampleRate > 0, format.channelCount > 0 else {
            Self.log.error("no microphone: input format \(format, privacy: .public)")
            throw AudioIOError.microphoneUnavailable
        }
        let gate = MicGate(encoder: try MicFrameEncoder(inputFormat: format), muted: muted, onFrame: onFrame)
        input.installTap(onBus: 0, bufferSize: Self.tapBufferSize, format: format, block: Self.makeTap(gate))
        tapInstalled = true
        Self.log.notice("microphone tap: \(format, privacy: .public)")
        return gate
    }

    /// `nonisolated` on purpose: a closure written inside a `@MainActor` method inherits the main
    /// actor, and Swift 6 traps when CoreAudio calls it on the audio thread.
    private nonisolated static func makeTap(_ gate: MicGate) -> AVAudioNodeTapBlock {
        { buffer, _ in gate.process(buffer) }
    }
}

#if DEBUG
/// Simulator only: the watch simulator has no microphone input (its input node reports 0 Hz).
/// Every 20 ms it hands the gate 960 frames at 48 kHz: a 440 Hz tone for 1 s, then 2 s of
/// silence, repeated. Loud enough for the server's VAD to close a turn after each tone.
final class SyntheticMic: @unchecked Sendable {
    static let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
    static let framesPerTick: AVAudioFrameCount = 960
    /// 50 ticks of tone (1 s), then 100 of silence (2 s).
    static let toneTicks = 50
    static let cycleTicks = 150

    private let gate: MicGate
    private let tick = Mutex(0)
    private let timer: DispatchSourceTimer

    init(gate: MicGate) {
        self.gate = gate
        timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "wristcall.synthetic-mic"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(20))
        timer.setEventHandler { [weak self] in self?.emit() }
        timer.resume()
    }

    func stop() {
        timer.cancel()
    }

    private func emit() {
        let index = tick.withLock { tick -> Int in
            defer { tick += 1 }
            return tick
        }
        gate.process(Self.buffer(tick: index))
    }

    static func buffer(tick: Int) -> AVAudioPCMBuffer {
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: framesPerTick)!
        buffer.frameLength = framesPerTick
        let samples = buffer.floatChannelData![0]
        let speaking = tick % cycleTicks < toneTicks
        for i in 0..<Int(framesPerTick) {
            let n = Float(tick * Int(framesPerTick) + i)
            samples[i] = speaking ? 0.3 * sin(2 * .pi * 440 * n / Float(format.sampleRate)) : 0
        }
        return buffer
    }
}
#endif
