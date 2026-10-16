import Foundation

/// The silent way into a Cloud account (spec O5): no screen, no password. The account key in iCloud Keychain is
/// the credential. On a device that already has a key, `run` signs in with it; on the first device it creates
/// the account. `migrate` gives a 0.3.x account (signed in with Zitadel) a key.
///
/// The one rule: **a key is on disk before it is sent**. Sign-up and the migration write the new key as the
/// pending item first, and only promote it to a synced key once the Cloud answered. So a reply lost on the
/// way (`409 key_in_use` / `key_exists` on the retry) is recovered with the same key, and nothing is stranded.
/// Nothing here deletes a synced key.
public struct AccountBootstrap: Sendable {
    public enum Outcome: Equatable, Sendable {
        /// A new account was created with a new key.
        case created
        /// This device signed in to an existing account with a key it already had (or a key whose sign-up reply was lost).
        case enrolled
        /// A 0.3.x account got its key.
        case migrated
    }

    let client: CloudClient
    let keys: any AccountKeyStore
    let device: CloudDeviceInfo
    /// `UserDefaults` is thread safe but the SDK does not mark it `Sendable`.
    nonisolated(unsafe) let defaults: UserDefaults
    let waitForKeys: @Sendable () async -> Void

    /// `waitForKeys`: how long a fresh install waits for iCloud Keychain to deliver the keys of the user's other
    /// devices before it makes a new account. Called at most once per `run`.
    public init(
        client: CloudClient, keys: any AccountKeyStore, device: CloudDeviceInfo, defaults: UserDefaults = .standard,
        waitForKeys: @escaping @Sendable () async -> Void = { try? await Task.sleep(for: .seconds(2)) }
    ) {
        self.client = client
        self.keys = keys
        self.device = device
        self.defaults = defaults
        self.waitForKeys = waitForKeys
    }

    /// Signs in with the first stored key the Cloud accepts, oldest first. A `401` marks that key dead (on
    /// this device; its item stays) and moves on. `nil` when no key is left to try. Any other error is thrown.
    public func enrollWithStoredKeys() async throws -> SessionTokens? {
        for stored in try keys.keys() where !DeadKeys.isDead(stored, defaults: defaults) {
            do {
                return try await client.enrollDevice(key: stored.key, account: stored.account, device: device)
            } catch APIError.unauthorized {
                DeadKeys.mark(stored, defaults: defaults)
            }
        }
        return nil
    }

    /// Signs in with a stored key, or creates the account. Throws on `429` (`APIError.retryLater`) and network
    /// errors without deleting anything: the pending key waits for the next call.
    public func run() async throws -> (SessionTokens, Outcome) {
        if let tokens = try await enrollWithStoredKeys() { return (tokens, .enrolled) }

        let pending = try keys.pending()
        if pending == nil {
            // No usable key here (none stored, or every one refused): the user's other devices may have an account
            // whose current key is still on its way through iCloud Keychain. Making a new account now would
            // leave the user with two.
            await waitForKeys()
            if let tokens = try await enrollWithStoredKeys() { return (tokens, .enrolled) }
        }

        let key = try pending ?? newPendingKey()
        do {
            let tokens = try await client.createAccount(key: key, device: device)
            try promote(key, to: tokens.account)
            return (tokens, .created)
        } catch APIError.conflict(let code, _) where code == "key_in_use" {
            // A sign-up whose reply was lost: the account exists with this very key.
            let tokens = try await client.enrollDevice(key: key, account: nil, device: device)
            try promote(key, to: tokens.account)
            return (tokens, .enrolled)
        }
    }

    /// Signs a 0.3.x account in through its Zitadel access token and, if the account has no key yet, gives it one.
    /// Returns the tokens of that first sign-in; the `PUT` uses its access token. A key the account already has
    /// is left alone.
    public func migrate(zitadelAccessToken: String) async throws -> SessionTokens {
        let tokens = try await client.enrollDevice(zitadelAccessToken: zitadelAccessToken, device: device)
        if tokens.keySet == true {
            await recoverLostKeyReply(for: tokens.account)
            return tokens
        }
        guard tokens.keySet == false else { return tokens }

        let key = try keys.pending() ?? newPendingKey()
        do {
            try await client.setAccountKey(key, accessToken: tokens.accessToken)
        } catch APIError.conflict(let code, _) where code == "key_exists" {
            // The `PUT` of a previous attempt got through and its reply did not: confirm the pending key is the one.
            // If the Cloud refuses it, the account's key is another one: the sign-in above still stands, and the
            // pending key stays for whoever looks at it next.
            do { try await confirmAndPromote(key, account: tokens.account) } catch APIError.unauthorized {}
            return tokens
        }
        try promote(key, to: tokens.account)
        return tokens
    }

    private func newPendingKey() throws -> AccountKey {
        let key = AccountKey.generate()
        try keys.savePending(key)
        return key
    }

    /// The Cloud confirmed `key` for `account`: it becomes a synced key and the pending item goes.
    private func promote(_ key: AccountKey, to account: String) throws {
        try keys.save(key, account: account)
        try keys.clearPending()
    }

    /// A `PUT` whose reply was lost leaves a pending key and, on the next attempt, an account that already has a
    /// key. If the pending key is the account's, promote it. Best effort: any failure leaves it for next time.
    private func recoverLostKeyReply(for account: String) async {
        // A Keychain read error is deliberately swallowed too: this is a bonus step, not the migration.
        guard let key = try? keys.pending() else { return }
        do { try await confirmAndPromote(key, account: account) } catch {}
    }

    /// Proves `key` is `account`'s key by signing in with it, promotes it, and signs the extra device out again
    /// (best effort: an unused device would count against the account's device limit).
    private func confirmAndPromote(_ key: AccountKey, account: String) async throws {
        let confirm = try await client.enrollDevice(key: key, account: account, device: device)
        try promote(key, to: account)
        try? await client.revokeDevice(id: confirm.device, accessToken: confirm.accessToken)
    }
}
