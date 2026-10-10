import Foundation
import Synchronization

/// Where `AccountSession` keeps the account's tokens.
public protocol TokenStore: Sendable {
    func load() throws -> TokenSet?
    func save(_ tokens: TokenSet) throws
    func delete() throws
}

/// Keeps the `TokenSet` as one JSON item in the Keychain, readable after the first unlock and never synced or
/// restored to another device. Errors are `CredentialStoreError`; `.keychain(errSecInteractionNotAllowed)` means
/// "not readable right now", not "signed out".
public struct KeychainTokenStore: TokenStore {
    public static let defaultService = "io.github.ggondim.wristcall.account"

    public let service: String
    public let account: String

    /// Tests pass their own `service` so they never touch the app's item.
    public init(service: String = Self.defaultService, account: String = "tokens") {
        self.service = service
        self.account = account
    }

    public func load() throws -> TokenSet? {
        guard let data = try item.read() else { return nil }
        guard let tokens = try? JSONDecoder().decode(TokenSet.self, from: data) else {
            throw CredentialStoreError.corruptedData
        }
        return tokens
    }

    public func save(_ tokens: TokenSet) throws {
        try item.write(try JSONEncoder().encode(tokens))
    }

    public func delete() throws {
        try item.delete()
    }

    private var item: KeychainItem { KeychainItem(service: service, account: account) }
}

/// A `TokenStore` in memory, for tests and previews.
public final class InMemoryTokenStore: TokenStore, Sendable {
    private let tokens: Mutex<TokenSet?>

    public init(_ tokens: TokenSet? = nil) {
        self.tokens = Mutex(tokens)
    }

    public func load() throws -> TokenSet? { tokens.withLock { $0 } }
    public func save(_ tokens: TokenSet) throws { self.tokens.withLock { $0 = tokens } }
    public func delete() throws { tokens.withLock { $0 = nil } }
}
