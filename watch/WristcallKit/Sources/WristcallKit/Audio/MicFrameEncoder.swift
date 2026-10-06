import AVFAudio
import Foundation

/// Turns microphone buffers, in whatever format the input node reports (48 kHz, 44.1 kHz, stereo,
/// Float32 or Int16), into the protocol's audio: PCM16 little-endian, mono, 16 kHz, in frames of exactly
/// `ProtocolConstants.frameBytes` (640 bytes, 20 ms). Audio that does not fill a frame waits for the next call.
///
/// Thread-safe: calls are serialized with a lock, so the audio tap may own it while another
/// context calls `reset()`.
public final class MicFrameEncoder: @unchecked Sendable {
    /// PCM16 interleaved mono 16 kHz.
    public static let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: Double(ProtocolConstants.inputSampleRate),
        channels: AVAudioChannelCount(ProtocolConstants.inputChannels),
        interleaved: true
    )!

    /// The format every buffer passed to `encode` must have.
    public let inputFormat: AVAudioFormat

    private let converter: AVAudioConverter
    private let lock = NSLock()
    private var pending = Data()

    /// Throws `AudioConversionError.unsupportedFormat` if `AVAudioConverter` cannot read `inputFormat`.
    public init(inputFormat: AVAudioFormat) throws {
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let converter = AVAudioConverter(from: inputFormat, to: Self.outputFormat)
        else { throw AudioConversionError.unsupportedFormat }
        // Stereo (or more) to mono: mix the channels instead of keeping only the first one.
        converter.downmix = true
        self.inputFormat = inputFormat
        self.converter = converter
    }

    /// Converts `buffer` and returns the frames completed so far (often 0, 1 or a few).
    /// Throws `AudioConversionError.formatMismatch` if `buffer.format != inputFormat`.
    public func encode(_ buffer: AVAudioPCMBuffer) throws -> [Data] {
        guard buffer.format == inputFormat else { throw AudioConversionError.formatMismatch }
        return try lock.withLock {
            try append(converter.convertAvailable(buffer, endOfStream: false))
            return takeFrames()
        }
    }

    /// End of speech (mute or hang-up): drains the converter and returns the remaining frames, the last
    /// one padded with silence. The encoder starts over afterwards.
    public func flush() throws -> [Data] {
        try lock.withLock {
            defer { converter.reset() }
            try append(converter.convertAvailable(nil, endOfStream: true))
            var frames = takeFrames()
            if !pending.isEmpty {
                pending.append(Data(count: ProtocolConstants.frameBytes - pending.count))
                frames.append(pending)
                pending = Data()
            }
            return frames
        }
    }

    /// Drops buffered audio without returning it (e.g. on unmute, so old audio is not sent).
    public func reset() {
        lock.withLock {
            converter.reset()
            pending = Data()
        }
    }

    private func append(_ buffers: [AVAudioPCMBuffer]) {
        for buffer in buffers {
            let bytes = Int(buffer.frameLength) * MemoryLayout<Int16>.size
            pending.append(Data(bytes: buffer.int16ChannelData![0], count: bytes))
        }
    }

    private func takeFrames() -> [Data] {
        let size = ProtocolConstants.frameBytes
        let count = pending.count / size
        guard count > 0 else { return [] }
        let frames = (0..<count).map { index in
            pending.subdata(in: pending.startIndex + index * size ..< pending.startIndex + (index + 1) * size)
        }
        pending = pending.subdata(in: pending.startIndex + count * size ..< pending.endIndex)
        return frames
    }
}
