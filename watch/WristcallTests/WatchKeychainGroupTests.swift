import Foundation
import Testing
import WristcallKit

@testable import Wristcall

/// The watch reads the account keys from the Keychain group it shares with the iPhone app (O6).
struct WatchKeychainGroupTests {
    @Test func sharedGroupIsReadable() throws {
        let group = KeychainGroup.shared(
            infoValue: Bundle.main.object(forInfoDictionaryKey: "WristcallKeychainGroup") as? String)
        let resolved = try #require(group)
        // Own services: the real account key is never touched.
        let store = KeychainAccountKeyStore(
            accessGroup: resolved, service: "io.github.ggondim.wristcall.account-key.test-o6",
            pendingService: "io.github.ggondim.wristcall.account-key.test-o6.pending")
        defer { try? store.deleteAll() }
        try store.save(AccountKey.generate(), account: "acc_test_o6")
        #expect(try store.keys().map(\.account) == ["acc_test_o6"])
        try store.delete(account: "acc_test_o6")
        #expect(try store.keys().isEmpty)
    }

    @Test func unexpandedGroupIsNil() {
        #expect(KeychainGroup.shared(infoValue: "") == nil)
        #expect(KeychainGroup.shared(infoValue: nil) == nil)
        #expect(KeychainGroup.shared(infoValue: "$(AppIdentifierPrefix)x") == nil)
        #expect(KeychainGroup.shared(infoValue: "ABCDE.shared") == "ABCDE.shared")
    }
}
