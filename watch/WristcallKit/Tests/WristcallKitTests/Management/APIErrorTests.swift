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

    @Test func planRequiredMaps402() {
        let body = Data(#"{"error":"plan_required","message":"x"}"#.utf8)
        #expect(APIError.error(status: 402, data: body, headers: [:]) == .planRequired)
        #expect(APIError.error(status: 402, data: Data(), headers: [:]) == .planRequired)
        #expect(APIError.planRequired.message == "This account has no Wristcall Cloud plan.")
    }

    @Test func noNodeAvailableMaps503() {
        let body = Data(#"{"error":"no_node_available","message":"m"}"#.utf8)
        #expect(APIError.error(status: 503, data: body, headers: ["retry-after": "30"]) == .noNodeAvailable(retryAfter: 30))
        #expect(APIError.error(status: 503, data: body, headers: [:]) == .noNodeAvailable(retryAfter: nil))
        #expect(APIError.error(status: 503, data: body, headers: ["retry-after": "soon"]) == .noNodeAvailable(retryAfter: nil))
        #expect(APIError.noNodeAvailable(retryAfter: 30).message
            == "Wristcall Cloud is temporarily unavailable. Try again in a minute.")
    }

    @Test func otherUnavailableStaysUnavailable() {
        let body = Data(#"{"error":"account_unavailable","message":"later"}"#.utf8)
        #expect(APIError.error(status: 503, data: body, headers: ["retry-after": "30"]) == .unavailable("later"))
        #expect(APIError.error(status: 502, data: Data(#"{"error":"no_node_available"}"#.utf8), headers: [:])
            == .unavailable("The server is unavailable. Try again later."))
    }
}
