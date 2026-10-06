import Testing
import WristcallKit
@testable import Wristcall

struct DigitPadModelTests {
    @Test func keepsAtMostEightDigits() {
        var pad = DigitPadModel()
        for digit in [1, 2, 3, 4, 5, 6, 7, 8, 9, 0] {
            pad.press(.digit(digit))
        }
        #expect(pad.digits == "12345678")
        #expect(pad.isComplete)
        #expect(pad.code == PairingCode("12345678"))
    }

    @Test func formatsLikeWristcallPair() {
        var pad = DigitPadModel()
        #expect(pad.formatted == "")
        for digit in [1, 2, 3, 4] {
            pad.press(.digit(digit))
        }
        #expect(pad.formatted == "1234")
        pad.press(.digit(5))
        #expect(pad.formatted == "1234 5")
        for digit in [6, 7, 8] {
            pad.press(.digit(digit))
        }
        #expect(pad.formatted == "1234 5678")
    }

    @Test func deleteAndClear() {
        var pad = DigitPadModel()
        pad.press(.delete)
        #expect(pad.digits == "")
        for digit in [9, 8, 7] {
            pad.press(.digit(digit))
        }
        pad.press(.delete)
        #expect(pad.digits == "98")
        #expect(!pad.isComplete)
        #expect(pad.code == nil)
        pad.press(.clear)
        #expect(pad.digits == "")
    }

    @Test func ignoresValuesThatAreNotOneDigit() {
        var pad = DigitPadModel()
        pad.press(.digit(10))
        pad.press(.digit(-1))
        #expect(pad.digits == "")
    }

    @Test func layoutIsThreeByFourWithZeroInTheMiddleOfTheLastRow() {
        #expect(DigitPadModel.layout.count == 4)
        #expect(DigitPadModel.layout.allSatisfy { $0.count == 3 })
        #expect(DigitPadModel.layout[0] == [.digit(1), .digit(2), .digit(3)])
        #expect(DigitPadModel.layout[3] == [.clear, .digit(0), .delete])
    }
}
