import Foundation

/// How a watch gets its Cloud device session in 0.5.0, without a screen: the account key in iCloud Keychain, then
/// the enroll code the iPhone sent, then the Zitadel login a 0.4.0 watch still has. Never creates an account.
///
/// The watch only reads account keys: it never writes or deletes a synced item (deleting on one device deletes on
/// all). A key the Cloud refuses (`401`) is marked dead on this watch (`DeadKeys`) and skipped from then on.
/// Secrets (keys, codes, tokens) are never logged.
public struct WatchEnrollment: Sendable {
    public enum Source: String, Sendable, Equatable {
        case accountKey, enrollCode, legacyAccount
    }

    public enum Result: Sendable, Equatable {
        case enrolled(SessionTokens, Source)
        /// Nothing worked right now (no key, code expired or refused, no old login): wait.
        case nothing
        /// `403 limit`: the account already has its 10 devices.
        case deviceLimit
    }

    let client: CloudClient
    let keys: any AccountKeyStore
    let legacy: AccountSession?
    let device: CloudDeviceInfo
    /// `UserDefaults` is thread safe but the SDK does not mark it `Sendable`.
    nonisolated(unsafe) let defaults: UserDefaults
    let now: @Sendable () -> Date

    public init(
        client: CloudClient, keys: any AccountKeyStore, legacy: AccountSession?, device: CloudDeviceInfo,
        defaults: UserDefaults = .standard, now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.client = client
        self.keys = keys
        self.legacy = legacy
        self.device = device
        self.defaults = defaults
        self.now = now
    }

    /// The keys (only the one of `code?.account` when it names an account), then `code` (if it has not
    /// expired), then the old Zitadel login if `tryLegacy`. Network and other errors are thrown; what is refused moves on to the next way.
    /// An old login is signed out once any way worked: the device session replaces it.
    public func enroll(code: PhoneLinkContext?, tryLegacy: Bool) async throws -> Result {
        let result = try await tryKeysThenCode(code, tryLegacy: tryLegacy)
        if case .enrolled = result { await dropLegacyLogin() }
        return result
    }

    /// Only the old login (the device flow just stored one). `.nothing` without a login, or when it is refused
    /// (the refused one is deleted).
    public func exchangeLegacy() async throws -> Result {
        let result = try await exchangeLegacyToken()
        if case .enrolled = result { await dropLegacyLogin() }
        return result
    }

    /// The device session replaces the old login. Nothing to do (and no call to the provider) when there is none.
    private func dropLegacyLogin() async {
        guard let legacy, await legacy.isSignedIn else { return }
        await legacy.signOut()
    }

    private func tryKeysThenCode(_ code: PhoneLinkContext?, tryLegacy: Bool) async throws -> Result {
        // A Keychain read error is no reason to stop: the code and the old login do not need the Keychain.
        let stored = (try? keys.keys()) ?? []
        // When the iPhone announces its account, only that account's key is tried: another key would put the
        // watch in an account other than the iPhone's. Without one, every key, oldest first.
        let announced = code?.account.flatMap { $0.isEmpty ? nil : $0 }
        let candidates = announced.map { account in stored.filter { $0.account == account } } ?? stored
        for key in candidates where !DeadKeys.isDead(key, defaults: defaults) {
            do {
                return .enrolled(try await client.enrollDevice(key: key.key, account: key.account, device: device), .accountKey)
            } catch APIError.unauthorized {
                DeadKeys.mark(key, defaults: defaults)
            } catch APIError.limit {
                return .deviceLimit
            }
        }

        // A code with under 5 s left would expire on the way: not sent.
        if let code, code.expiresAt - 5 > now().timeIntervalSince1970 {
            do {
                return .enrolled(try await client.enrollDevice(code: code.enrollCode, device: device), .enrollCode)
            } catch APIError.invalidCode {
                // Spent or expired: the iPhone will send another.
            } catch APIError.forbidden {
                // Refused (not a watch code): wait for another.
            } catch APIError.limit {
                return .deviceLimit
            }
        }

        return tryLegacy ? try await exchangeLegacyToken() : .nothing
    }

    private func exchangeLegacyToken() async throws -> Result {
        guard let legacy else { return .nothing }
        let token: String
        do {
            token = try await legacy.freshAccessToken()
        } catch AccountError.signedOut {
            return .nothing
        }
        do {
            return .enrolled(try await client.enrollDevice(zitadelAccessToken: token, device: device), .legacyAccount)
        } catch APIError.staleToken, APIError.unauthorized, APIError.invalidAccountToken, APIError.forbidden {
            await legacy.signOut()
            return .nothing
        } catch APIError.limit {
            return .deviceLimit
        }
    }
}
