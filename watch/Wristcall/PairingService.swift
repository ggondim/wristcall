import Foundation
import WristcallKit

/// What `AppModel` needs from `PairingClient`. Tests replace it with a stub.
protocol PairingService: Sendable {
    func resolve(code: PairingCode, directory: URL) async throws -> URL
    func pair(server: URL, code: PairingCode?, deviceName: String) async throws -> PairResult
    func poll(server: URL, pollToken: String) async throws -> PollResult
    func me(server: URL, token: String) async throws -> DeviceInfo
    func unpair(server: URL, token: String) async throws
    func callStatus(server: URL, token: String, callID: String) async throws -> CallStatus
}

extension PairingClient: PairingService {}
