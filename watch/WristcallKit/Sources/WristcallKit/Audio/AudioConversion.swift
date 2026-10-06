import AVFAudio
import Foundation

#if _endian(big)
#error("The protocol's PCM16 is little-endian; MicFrameEncoder and PlaybackDecoder copy samples as stored in memory.")
#endif

/// Errors of `MicFrameEncoder` and `PlaybackDecoder`.
public enum AudioConversionError: Error, Sendable, Equatable {
    /// `AVAudioConverter` cannot convert between these formats (or the rate is not positive).
    case unsupportedFormat
    /// The buffer is not in the format the encoder was created with (e.g. the input route changed):
    /// create a new encoder with the new format.
    case formatMismatch
    /// `AVAudioConverter` reported an error.
    case conversionFailed(String)
}

extension AVAudioConverter {
    /// Pushes `input` (or nothing) through the converter and returns everything it produces now.
    ///
    /// With `endOfStream == false` the converter keeps its state (resampler history) for the next call,
    /// which is what makes chunked audio sound continuous. With `true` it drains what it holds back;
    /// call `reset()` before using it again.
    func convertAvailable(_ input: AVAudioPCMBuffer?, endOfStream: Bool) throws -> [AVAudioPCMBuffer] {
        let ratio = outputFormat.sampleRate / inputFormat.sampleRate
        let inputFrames = Double(input?.frameLength ?? 0)
        // Room for this input plus what the resampler held back from earlier calls.
        let capacity = AVAudioFrameCount((inputFrames * ratio).rounded(.up)) + 1_024
        let feeder = InputFeeder(buffer: input, endOfStream: endOfStream)
        var outputs: [AVAudioPCMBuffer] = []
        while true {
            guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
                throw AudioConversionError.unsupportedFormat
            }
            var error: NSError?
            let status = convert(to: output, error: &error, withInputFrom: feeder.next)
            if output.frameLength > 0 {
                outputs.append(output)
            }
            switch status {
            case .haveData:
                // The output buffer is full; there is more.
                continue
            case .inputRanDry, .endOfStream:
                return outputs
            case .error:
                throw AudioConversionError.conversionFailed(error?.localizedDescription ?? "unknown error")
            @unknown default:
                throw AudioConversionError.conversionFailed("unknown status \(status.rawValue)")
            }
        }
    }
}

/// Hands one buffer to `AVAudioConverter`, then reports "no data now" (more audio will come)
/// or "end of stream" (drain). The converter calls it synchronously, inside `convert`.
private final class InputFeeder {
    private var buffer: AVAudioPCMBuffer?
    private let endOfStream: Bool

    init(buffer: AVAudioPCMBuffer?, endOfStream: Bool) {
        self.buffer = buffer?.frameLength == 0 ? nil : buffer
        self.endOfStream = endOfStream
    }

    func next(_: AVAudioPacketCount, _ status: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        if let buffer {
            self.buffer = nil
            status.pointee = .haveData
            return buffer
        }
        status.pointee = endOfStream ? .endOfStream : .noDataNow
        return nil
    }
}

extension AVAudioPCMBuffer {
    /// One buffer with the frames of `buffers`, all in `format`.
    static func concatenating(_ buffers: [AVAudioPCMBuffer], format: AVAudioFormat) throws -> AVAudioPCMBuffer {
        if buffers.count == 1 { return buffers[0] }
        let total = buffers.reduce(0) { $0 + $1.frameLength }
        guard let result = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: max(total, 1)) else {
            throw AudioConversionError.unsupportedFormat
        }
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        let destination = UnsafeMutableAudioBufferListPointer(result.mutableAudioBufferList)
        var offset = 0
        for buffer in buffers {
            let source = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
            let bytes = Int(buffer.frameLength) * bytesPerFrame
            for index in 0..<min(source.count, destination.count) {
                guard let from = source[index].mData, let to = destination[index].mData else { continue }
                (to + offset).copyMemory(from: from, byteCount: bytes)
            }
            offset += bytes
        }
        result.frameLength = total
        return result
    }
}
