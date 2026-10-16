/// A single-use link code from a wristcall server's link URL (protocol v1: 16 random bytes in base32, RFC 4648,
/// upper case, no padding, so 26 characters of `A`-`Z` and `2`-`7`; spaces and hyphens are ignored).
public struct LinkCode: Sendable, Hashable {
    /// Exactly 26 characters of `A`-`Z2`-`7`.
    public let value: String

    /// `nil` unless `raw` is a link code once spaces, hyphens and letter case are normalized.
    public init?(_ raw: String) {
        guard let value = Self.normalize(raw) else { return nil }
        self.value = value
    }

    /// The code of `raw` upper-cased and without spaces (any whitespace) and hyphens, or `nil` if anything
    /// else is left or the count is not 26. Only ASCII letters and the digits `2`-`7` count (no `0`, `1`, `8`,
    /// `9`, `=`, no other scripts).
    public static func normalize(_ raw: String) -> String? {
        var value = ""
        for character in raw {
            if character == "-" || character.isWhitespace { continue }
            guard character.isASCII, let ascii = character.asciiValue else { return nil }
            let upper = (ascii >= 0x61 && ascii <= 0x7A) ? ascii - 0x20 : ascii
            let isLetter = upper >= 0x41 && upper <= 0x5A
            let isDigit = upper >= 0x32 && upper <= 0x37
            guard isLetter || isDigit else { return nil }
            value.append(Character(UnicodeScalar(upper)))
        }
        return value.count == 26 ? value : nil
    }
}

/// The code is a one-time secret: nothing that prints a value (`print`, `dump`, a failed `#expect`) shows it.
extension LinkCode: CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var description: String { "LinkCode(<redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["value": "<redacted>"]) }
}
