#if os(macOS)
import Foundation
import Synchronization
import WristcallKit

/// Paired devices for the integration tests.
///
/// The server allows 10 `POST /v1/pair` per minute per IP. Per run of the whole suite:
/// `PairingIntegrationTests` makes 3 on its own; through this helper, `shared()` makes 1
/// (cached for the whole test process, reused by every call test) and
/// `TransportIntegrationTests.revokedTokenIsClosedWith4401` 1. Total: 5.
/// `pair(name:)` refuses to go over `maxPairingsPerRun`, so a new test cannot silently
/// push the run into `rateLimited`.
enum TestDevices {
    static let maxPairingsPerRun = 3

    private static let pairings = Mutex(0)
    private static let cache = SharedDevice()

    /// One device paired once per test process and reused by every call test. Never revoke it.
    static func shared() async throws -> PairedDevice {
        try await cache.device()
    }

    /// Pairs a new device with a fresh code from `wristcall pair`.
    static func pair(name: String) async throws -> PairedDevice {
        let count = pairings.withLock { count -> Int in
            count += 1
            return count
        }
        guard count <= maxPairingsPerRun else {
            throw TestServer.CLIError(description: "more than \(maxPairingsPerRun) pairings in one run (server limit: 10/min/IP)")
        }
        let server = try TestServer.requireBaseURL()
        guard let code = PairingCode(try TestServer.newPairingCode()) else {
            throw TestServer.CLIError(description: "wristcall pair printed an invalid code")
        }
        let result = try await PairingClient().pair(server: server, code: code, deviceName: name)
        guard case .paired(let device) = result else {
            throw TestServer.CLIError(description: "expected .paired, got \(result)")
        }
        return device
    }

    /// `wristcall devices revoke <deviceId>`.
    static func revoke(_ device: PairedDevice) throws {
        _ = try TestServer.runCLI(["devices", "revoke", device.deviceId])
    }

    private actor SharedDevice {
        private var pairing: Task<PairedDevice, any Error>?

        func device() async throws -> PairedDevice {
            if let pairing {
                return try await pairing.value
            }
            let task = Task { try await TestDevices.pair(name: "Integration Watch (shared)") }
            pairing = task
            return try await task.value
        }
    }
}

extension TestServer {
    static func requireBaseURL() throws -> URL {
        guard let url = TestServer.baseURL else {
            throw TestServer.CLIError(description: "WRISTCALL_TEST_SERVER not set")
        }
        return url
    }
}
#endif
