import AVFAudio
import Foundation

/// Turns the server's audio (binary WebSocket frames of PCM16 little-endian mono at
/// `session.ready.audio_out.sample_rate`) into buffers in the player's format.
///
/// A frame may end in the middle of a sample: the odd byte is kept and joined with the next call.
/// Thread-safe: calls are serialized with a lock.
public final class PlaybackDecoder: @unchecked Sendable {
    /// PCM16 interleaved mono at the server's rate.
    public let inputFormat: AVAudioFormat
    /// The format of every returned buffer.
    public let outputFormat: AVAudioFormat

    private let converter: AVAudioConverter
    private let lock = NSLock()
    private var carry: UInt8?

    /// Throws `AudioConversionError.unsupportedFormat` for a rate that is not positive or a format
    /// `AVAudioConverter` cannot produce.
    public init(sampleRate: Int, outputFormat: AVAudioFormat) throws {
        guard sampleRate > 0, outputFormat.sampleRate > 0, outputFormat.channelCount > 0,
              let inputFormat = AVAudioFormat(
                  commonFormat: .pcmFormatInt16,
                  sampleRate: Double(sampleRate),
                  channels: 1,
                  interleaved: true
              ),
              let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
        else { throw AudioConversionError.unsupportedFormat }
        // Mono to stereo needs no setup: AVAudioConverter copies the channel to both sides (tested).
        self.inputFormat = inputFormat
        self.outputFormat = outputFormat
        self.converter = converter
    }

    /// Converts `data` (plus a byte carried from the previous call). The buffer may be empty
    /// (`frameLength == 0`) when there was not a whole sample yet; do not schedule those.
    public func decode(_ data: Data) throws -> AVAudioPCMBuffer {
        try lock.withLock {
            var bytes = Data(capacity: data.count + 1)
            if let carry { bytes.append(carry) }
            bytes.append(data)
            if bytes.count % 2 == 1 {
                carry = bytes.removeLast()
            } else {
                carry = nil
            }
            guard !bytes.isEmpty else { return try emptyBuffer() }

            let frames = AVAudioFrameCount(bytes.count / 2)
            guard let input = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frames) else {
                throw AudioConversionError.unsupportedFormat
            }
            input.frameLength = frames
            bytes.withUnsafeBytes { raw in
                UnsafeMutableRawPointer(input.int16ChannelData![0]).copyMemory(
                    from: raw.baseAddress!,
                    byteCount: Int(frames) * 2
                )
            }
            return try output(converter.convertAvailable(input, endOfStream: false))
        }
    }

    /// End of the agent's turn: returns what the resampler still holds (a few milliseconds) and starts over.
    public func flush() throws -> AVAudioPCMBuffer {
        try lock.withLock {
            defer {
                converter.reset()
                carry = nil
            }
            return try output(converter.convertAvailable(nil, endOfStream: true))
        }
    }

    /// Drops the carried byte and the resampler state (e.g. after the user interrupts the agent).
    public func reset() {
        lock.withLock {
            converter.reset()
            carry = nil
        }
    }

    private func output(_ buffers: [AVAudioPCMBuffer]) throws -> AVAudioPCMBuffer {
        buffers.isEmpty ? try emptyBuffer() : try AVAudioPCMBuffer.concatenating(buffers, format: outputFormat)
    }

    private func emptyBuffer() throws -> AVAudioPCMBuffer {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: 1) else {
            throw AudioConversionError.unsupportedFormat
        }
        return buffer
    }
}
