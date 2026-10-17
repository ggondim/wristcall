import Foundation
import Testing
import WristcallKit
@testable import Wristcall

struct SetupErrorsTests {
    @Test func signedOutCases() {
        // The session is gone (or the Cloud no longer knows this device): the watch enrolls again.
        #expect(SetupErrors.isSignedOut(AccountError.signedOut))
        #expect(SetupErrors.isSignedOut(APIError.unauthorized))
        #expect(SetupErrors.isSignedOut(APIError.revoked))
        // A network failure keeps the session.
        #expect(!SetupErrors.isSignedOut(APIError.network(.timedOut)))
    }
}
