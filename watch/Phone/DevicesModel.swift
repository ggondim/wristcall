import Foundation
import Observation
import WristcallKit

extension APIError {
    /// The text the screens show: the server's own words for a refusal (`limit`, `unavailable`, …).
    static func text(_ error: any Error) -> String {
        (error as? APIError)?.message ?? "Can't reach the server."
    }
}

/// The devices (watches) paired to one server, and the pairing code the owner can ask for.
@MainActor
@Observable
final class DevicesModel {
    private(set) var devices: [DeviceRecord] = []
    var error: String?

    /// The code on screen, as the server issued it. It is a secret for 10 minutes: never logged, never in
    /// `error`, and gone from the screen once it expires.
    private var grant: PairingCodeGrant?
    @ObservationIgnored private let api: any ServerAPI
    @ObservationIgnored private let now: () -> Date

    init(api: any ServerAPI, now: @escaping () -> Date = Date.init) {
        self.api = api
        self.now = now
    }

    /// The pairing code on screen; `nil` when there is none or it has expired. Reads the clock each time
    /// (the view asks again every second).
    var code: PairingCodeGrant? {
        guard let grant, now().timeIntervalSince1970 < grant.expiresAt else { return nil }
        return grant
    }

    func load() async {
        do {
            devices = try await api.devices()
            error = nil
        } catch {
            self.error = APIError.text(error)
        }
    }

    /// Revokes the device's credential on the server. A device that is already gone counts as revoked.
    func revoke(_ device: DeviceRecord) async {
        do {
            try await api.revokeDevice(device.id)
        } catch APIError.notFound {
            // Revoked from elsewhere meanwhile: same result.
        } catch {
            self.error = APIError.text(error)
            return
        }
        error = nil
        devices.removeAll { $0.id == device.id }
    }

    /// Asks for a new 8 digit code. A failure (device limit, directory down) clears the previous code too:
    /// what is on screen is always what the server last issued.
    func newPairingCode() async {
        do {
            grant = try await api.createPairingCode()
            error = nil
        } catch {
            grant = nil
            self.error = APIError.text(error)
        }
    }

    /// Seconds until the code expires (0 when there is none).
    func secondsLeft() -> Int {
        guard let grant else { return 0 }
        return max(0, Int((grant.expiresAt - now().timeIntervalSince1970).rounded(.up)))
    }
}
