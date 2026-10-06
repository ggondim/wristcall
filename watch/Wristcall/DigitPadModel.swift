import WristcallKit

/// State of the pairing code keypad: up to 8 digits.
struct DigitPadModel: Equatable {
    enum Key: Hashable {
        case digit(Int)
        case delete
        case clear
    }

    static let maxDigits = 8
    /// The 3x4 grid shown by `DigitPad`, top row first.
    static let layout: [[Key]] = [
        [.digit(1), .digit(2), .digit(3)],
        [.digit(4), .digit(5), .digit(6)],
        [.digit(7), .digit(8), .digit(9)],
        [.clear, .digit(0), .delete],
    ]

    private(set) var digits = ""

    /// Digits in groups of 4, as `wristcall pair` prints them: `"1234 5678"`, `"1234 5"`.
    var formatted: String {
        digits.count > 4 ? "\(digits.prefix(4)) \(digits.dropFirst(4))" : digits
    }

    var isComplete: Bool { digits.count == Self.maxDigits }

    /// The code once all 8 digits are in.
    var code: PairingCode? { PairingCode(digits) }

    mutating func press(_ key: Key) {
        switch key {
        case .digit(let value):
            guard (0...9).contains(value), digits.count < Self.maxDigits else { return }
            digits.append(String(value))
        case .delete:
            if !digits.isEmpty { digits.removeLast() }
        case .clear:
            digits = ""
        }
    }
}
