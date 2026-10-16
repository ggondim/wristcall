import Foundation
import Testing
import WristcallKit

/// The silent account lifecycle against a real Cloud 0.4.0 (a local one with a session signing key; never the
/// production Cloud). Skipped unless `WRISTCALL_TEST_CLOUD_SESSIONS` is set to its URL (for example
/// `http://127.0.0.1:8090`); never runs in CI. It makes a throwaway account and always deletes it.
///
/// Nothing here prints a key, a token or a code, and none goes inside `#expect`: a failed expectation prints
/// the values of its parts, so secrets are compared first and only the `Bool` is expected.
@Suite(.serialized, .enabled(if: CloudSessionEnvironment.isConfigured, "set WRISTCALL_TEST_CLOUD_SESSIONS to run the session tests"))
struct CloudSessionIntegrationTests {
    static let phone = CloudDeviceInfo(name: "o5 e2e iPhone", platform: .ios)

    @Test func fullSilentAccountLifecycle() async throws {
        let cloud = CloudClient(cloud: try CloudSessionEnvironment.requireCloud())
        let key = AccountKey.generate()
        do {
            try await Self.lifecycle(cloud, key)
        } catch {
            await Self.deleteAccount(cloud, key)
            throw error
        }
        await Self.deleteAccount(cloud, key)
    }

    private static func lifecycle(_ cloud: CloudClient, _ key: AccountKey) async throws {
        // A silent account: key and device, nothing else.
        let first = try await cloud.createAccount(key: key, device: phone)
        #expect(first.account.hasPrefix("acc_"))
        #expect(first.expiresAt > Date())

        // Refresh rotates the token; spending the old one again signs the device out.
        let renewed = try await cloud.refreshSession(first.refreshToken)
        let rotated = renewed.refreshToken != first.refreshToken
        #expect(rotated)
        do {
            _ = try await cloud.refreshSession(first.refreshToken)
            Issue.record("a spent refresh token was accepted")
        } catch let error as APIError {
            #expect(error == .revoked)
        }

        // The key brings a new device back into the same account.
        let again = try await cloud.enrollDevice(key: key, device: phone)
        #expect(again.account == first.account)
        #expect(again.keySet == true)
        let token = again.accessToken

        let info = try await cloud.account(accessToken: token)
        #expect(info.account == first.account)
        #expect(info.plan == nil)
        #expect(info.isEmpty)
        #expect(info.keySet)

        let devices = try await cloud.devices(accessToken: token)
        #expect(devices.contains { $0.id == again.device && $0.current })

        let code = try await cloud.enrollCode(accessToken: token)
        let hasCode = !code.code.isEmpty
        #expect(hasCode)
        #expect(code.expiresAt > Date())

        let audience = try #require(URL(string: "https://o5-e2e.wristcall.invalid"))
        let serverToken = try await cloud.serverToken(audience: audience, accessToken: token)
        let hasServerToken = !serverToken.token.isEmpty
        #expect(hasServerToken)

        // An invite that does not exist.
        do {
            _ = try await cloud.redeemInvite("WC-AAAA-BBBB-CCCC", accessToken: token)
            Issue.record("an unknown invite was accepted")
        } catch let error as APIError {
            #expect(error == .invalidCode)
        }

        // Deleting the account makes the key useless.
        try await cloud.deleteAccount(accessToken: token)
        do {
            _ = try await cloud.enrollDevice(key: key, device: phone)
            Issue.record("the key of a deleted account was accepted")
        } catch let error as APIError {
            #expect(error == .unauthorized)
        }
    }

    /// Best effort and always awaited: signs in again with the key and deletes the account. After a test that
    /// already deleted it the key is refused and there is nothing to do.
    private static func deleteAccount(_ cloud: CloudClient, _ key: AccountKey) async {
        guard let session = try? await cloud.enrollDevice(key: key, device: phone) else { return }
        try? await cloud.deleteAccount(accessToken: session.accessToken)
    }
}

enum CloudSessionEnvironment {
    static let cloud = ProcessInfo.processInfo.environment["WRISTCALL_TEST_CLOUD_SESSIONS"].flatMap { URL(string: $0) }
    static var isConfigured: Bool { cloud != nil }

    struct Failure: Error, CustomStringConvertible {
        let description: String
    }

    static func requireCloud() throws -> URL {
        guard let cloud else { throw Failure(description: "WRISTCALL_TEST_CLOUD_SESSIONS not set") }
        return cloud
    }
}
