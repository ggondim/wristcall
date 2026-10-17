import Foundation
import WristcallKit

/// Errors that mean the watch's Cloud session is gone: enroll again instead of trying later.
enum SetupErrors {
    static func isSignedOut(_ error: any Error) -> Bool {
        switch error {
        case AccountError.signedOut, APIError.unauthorized, APIError.revoked: true
        default: false
        }
    }
}

/// `SetupModel`'s Cloud: the app's one `DeviceSession` (one refresh at a time), the Kit's `WatchEnrollment` and an
/// `AccountPairing` over that session. Nothing here logs; keys, codes and tokens never leave the Kit's calls.
struct LiveSetupBackend: SetupBackend {
    let session: DeviceSession
    let enrollment: WatchEnrollment
    let pairing: AccountPairing

    func restore() async -> String? {
        await session.isSignedIn ? await session.accountID : nil
    }

    func enroll(code: PhoneLinkContext?, tryLegacy: Bool) async throws -> SetupEnrollResult {
        switch try await enrollment.enroll(code: code, tryLegacy: tryLegacy) {
        case .enrolled(let tokens, _):
            try await session.start(tokens)
            return .enrolled(account: tokens.account)
        case .nothing:
            return .nothing
        case .deviceLimit:
            return .deviceLimit
        }
    }

    func agenda() async throws -> [CloudServer] {
        do {
            return try await session.cloudClient.servers(accessToken: try await session.accessToken())
        } catch where SetupErrors.isSignedOut(error) {
            // The Cloud no longer knows this device: the saved session is useless, the setup enrolls again.
            try? await session.signOut()
            throw AccountError.signedOut
        }
    }

    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome {
        await pairing.pair(server, deviceName: deviceName, allowApproval: false)
    }

    func signOut() async {
        // Best effort: without the Cloud (offline, session already refused) the device stays there until removed
        // from the iPhone; the local session goes either way.
        if let device = await session.deviceID, let token = try? await session.accessToken() {
            try? await session.cloudClient.revokeDevice(id: device, accessToken: token)
        }
        try? await session.signOut()
    }
}
