import AVFAudio
import Foundation

/// Synthetic signals for the audio tests: no microphone, no files.
enum AudioTestSignals {
    /// A Float32 buffer (deinterleaved, like the `AVAudioEngine` input node) with a sine of
    /// `amplitude` on every channel. `phaseOffset` continues a sine from a previous buffer.
    static func sine(
        sampleRate: Double,
        channels: AVAudioChannelCount,
        frames: AVAudioFrameCount,
        frequency: Double = 440,
        amplitude: Float = 0.5,
        phaseOffset: AVAudioFramePosition = 0
    ) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: channels)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for channel in 0..<Int(channels) {
            let samples = buffer.floatChannelData![channel]
            for frame in 0..<Int(frames) {
                let time = Double(phaseOffset + AVAudioFramePosition(frame)) / sampleRate
                samples[frame] = amplitude * Float(sin(2 * .pi * frequency * time))
            }
        }
        return buffer
    }

    /// `seconds` of sine split in buffers of `chunk` frames (the last one may be shorter), as a tap delivers them.
    static func sineChunks(
        sampleRate: Double,
        channels: AVAudioChannelCount,
        seconds: Double,
        chunk: AVAudioFrameCount
    ) -> [AVAudioPCMBuffer] {
        let total = AVAudioFramePosition(sampleRate * seconds)
        var chunks: [AVAudioPCMBuffer] = []
        var position: AVAudioFramePosition = 0
        while position < total {
            let frames = AVAudioFrameCount(min(AVAudioFramePosition(chunk), total - position))
            chunks.append(sine(sampleRate: sampleRate, channels: channels, frames: frames, phaseOffset: position))
            position += AVAudioFramePosition(frames)
        }
        return chunks
    }

    /// Mono PCM16 little-endian bytes of a sine, as the server sends them.
    static func pcm16Sine(sampleRate: Int, seconds: Double, frequency: Double = 440, amplitude: Double = 0.5) -> Data {
        let count = Int(Double(sampleRate) * seconds)
        var data = Data(capacity: count * 2)
        for index in 0..<count {
            let value = Int16(amplitude * 32767 * sin(2 * .pi * frequency * Double(index) / Double(sampleRate)))
            withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
        }
        return data
    }

    /// Samples of PCM16 little-endian bytes.
    static func samples(_ data: Data) -> [Int16] {
        stride(from: 0, to: data.count - 1, by: 2).map { offset in
            Int16(littleEndian: Int16(bitPattern: UInt16(data[data.startIndex + offset]) | UInt16(data[data.startIndex + offset + 1]) << 8))
        }
    }

    /// Root mean square of PCM16 samples, scaled to 0...1.
    static func rms(_ samples: [Int16]) -> Double {
        guard !samples.isEmpty else { return 0 }
        let sum = samples.reduce(0.0) { $0 + Double($1) * Double($1) }
        return (sum / Double(samples.count)).squareRoot() / 32768
    }

    /// Root mean square of one channel of a Float32 buffer.
    static func rms(_ buffer: AVAudioPCMBuffer, channel: Int) -> Double {
        let samples = buffer.floatChannelData![channel]
        let count = Int(buffer.frameLength)
        guard count > 0 else { return 0 }
        let sum = (0..<count).reduce(0.0) { $0 + Double(samples[$1]) * Double(samples[$1]) }
        return (sum / Double(count)).squareRoot()
    }
}
