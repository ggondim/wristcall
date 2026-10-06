/// An 8 digit pairing code (protocol v1: spaces and hyphens are ignored).
public struct PairingCode: Sendable, Hashable {
    /// Exactly 8 ASCII digits, e.g. `"12345678"`.
    public let digits: String

    /// `nil` unless `raw` is 8 ASCII digits once spaces and hyphens are removed.
    public init?(_ raw: String) {
        guard let digits = Self.normalize(raw) else { return nil }
        self.digits = digits
    }

    /// `"1234 5678"`, as `wristcall pair` prints it.
    public var formatted: String {
        "\(digits.prefix(4)) \(digits.suffix(4))"
    }

    /// The 8 digits of `raw` without spaces (any whitespace) and hyphens, or `nil` if anything
    /// else is left or the count is not 8. Only ASCII digits count: "١٢٣٤٥٦٧٨" is rejected.
    public static func normalize(_ raw: String) -> String? {
        var digits = ""
        for character in raw {
            if character == "-" || character.isWhitespace { continue }
            guard character.isASCII, character.isWholeNumber else { return nil }
            digits.append(character)
        }
        return digits.count == 8 ? digits : nil
    }
}
