import Foundation
import Testing
@testable import WristcallKit

struct APIErrorTests {
    private func body(_ code: String) -> Data { Data(#"{"error":"\#(code)"}"#.utf8) }

    @Test func sessionCodes() {
        #expect(APIError.error(status: 401, data: body("revoked"), headers: [:]) == .revoked)
        #expect(APIError.error(status: 401, data: body("stale_token"), headers: [:]) == .staleToken)
        #expect(APIError.error(status: 404, data: body("invalid_code"), headers: [:]) == .invalidCode)
        #expect(APIError.error(status: 401, data: body("invalid_code"), headers: [:]) == .invalidCode)
        #expect(APIError.error(status: 404, data: Data(), headers: [:]) == .notFound)
    }

    @Test func retryAfterBecomesRetryLater() {
        let error = APIError.error(status: 429, data: Data(), headers: ["retry-after": "120"])
        #expect(error == .retryLater(120))
        #expect(error.message == "Too many attempts. Try again in 2 minutes.")
        #expect(APIError.error(status: 429, data: Data(), headers: [:]) == .rateLimited)
        #expect(APIError.error(status: 429, data: Data()) == .rateLimited)
        #expect(APIError.error(status: 429, data: Data(), headers: ["retry-after": "soon"]) == .rateLimited)
    }

    @Test func retryLaterRoundsUpToAtLeastOneMinute() {
        #expect(APIError.retryLater(1).message == "Too many attempts. Try again in 1 minute.")
        #expect(APIError.retryLater(0).message == "Too many attempts. Try again in 1 minute.")
        #expect(APIError.retryLater(61).message == "Too many attempts. Try again in 2 minutes.")
    }

    @Test(arguments: ["inf", "-inf", "nan", "1e30", "infinity"])
    func malformedRetryAfterNeverCrashes(value: String) {
        let error = APIError.error(status: 429, data: Data(), headers: ["retry-after": value])
        if case .retryLater = error {
            #expect(!error.message.isEmpty)
        } else {
            #expect(error == .rateLimited)
        }
    }

    @Test func extremeRetryLaterValuesAreClamped() {
        #expect(APIError.retryLater(.infinity).message == "Too many attempts. Try again in 1440 minutes.")
        #expect(APIError.retryLater(1e30).message == "Too many attempts. Try again in 1440 minutes.")
        #expect(APIError.retryLater(.nan).message.hasPrefix("Too many attempts."))
    }
}
