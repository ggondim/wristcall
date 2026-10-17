import Observation
import SwiftUI
import WristcallKit

/// The devices signed in to the account (`GET /v1/devices`): revoke one, or sign every other out.
@MainActor
@Observable
final class AccountDevicesModel {
    private(set) var devices: [AccountDevice] = []
    /// The list was read at least once.
    private(set) var didLoad = false
    private(set) var busy = false
    private(set) var error: String?
    /// What the last "Sign out everywhere" did.
    private(set) var notice: String?

    @ObservationIgnored private let account: AccountModel

    init(account: AccountModel) {
        self.account = account
    }

    /// "Sign out everywhere" needs this account's key on this iPhone (it rotates the key).
    var offersSignOutEverywhere: Bool { account.hasLocalKey }

    /// This iPhone never revokes itself (it would sign itself out): no swipe action on the `current` one.
    nonisolated static func canRevoke(_ device: AccountDevice) -> Bool { !device.current }

    func load() async {
        do {
            let list = try await account.devices()
            devices = Self.sorted(list)
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = AccountModel.message(for: error)
        }
        didLoad = true
    }

    func revoke(_ device: AccountDevice) async {
        guard Self.canRevoke(device), !busy else { return }
        busy = true
        defer { busy = false }
        notice = nil
        do {
            try await account.revokeDevice(id: device.id)
            devices.removeAll { $0.id == device.id }
            error = nil
        } catch is CancellationError {
        } catch {
            self.error = AccountModel.message(for: error)
        }
    }

    func signOutEverywhere() async {
        guard offersSignOutEverywhere, !busy else { return }
        busy = true
        defer { busy = false }
        notice = nil
        do {
            let count = try await account.signOutEverywhere()
            notice = Self.signedOutNotice(count)
            error = nil
        } catch is CancellationError {
            return
        } catch {
            self.error = AccountModel.message(for: error)
            return
        }
        await load()
    }

    // MARK: - Text

    /// This iPhone first, then the most recently used.
    nonisolated static func sorted(_ list: [AccountDevice]) -> [AccountDevice] {
        list.sorted { left, right in
            if left.current != right.current { return left.current }
            return (left.lastSeenAt ?? left.createdAt) > (right.lastSeenAt ?? right.createdAt)
        }
    }

    /// A device's name on one line: names come from the device (or the web browser) and may hold line
    /// breaks, tabs or the Unicode line and paragraph separators (U+2028, U+2029).
    nonisolated static func oneLine(_ name: String) -> String {
        let breaks = CharacterSet.newlines.union(.controlCharacters)
        let words = name.components(separatedBy: breaks).flatMap { $0.split(separator: " ") }
        let text = words.joined(separator: " ")
        return text.isEmpty ? "Device" : text
    }

    nonisolated static func platformName(_ platform: String) -> String {
        switch platform {
        case "ios": "iPhone"
        case "ipados": "iPad"
        case "macos": "Mac"
        case "watchos": "Apple Watch"
        case "web": "Web"
        default: platform
        }
    }

    /// "This iPhone", or the platform and when the device was last used.
    nonisolated static func subtitle(for device: AccountDevice, now: Date = Date()) -> String {
        if device.current { return "This iPhone" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        let name = platformName(device.platform)
        guard let seen = device.lastSeenAt else {
            return "\(name) · added " + formatter.localizedString(for: device.createdAt, relativeTo: now)
        }
        return "\(name) · last used " + formatter.localizedString(for: seen, relativeTo: now)
    }

    nonisolated static func signedOutNotice(_ count: Int) -> String {
        switch count {
        case 0: "No other device was signed in."
        case 1: "Signed out 1 other device."
        default: "Signed out \(count) other devices."
        }
    }

    nonisolated static let signOutEverywhereMessage =
        "Sign out every other device? They need iCloud Keychain or a new sign-in to come back."
}

struct AccountDevicesView: View {
    let model: AccountDevicesModel
    @State private var confirmingSignOut = false

    var body: some View {
        List {
            if let error = model.error {
                Section { Text(error).foregroundStyle(.red) }
            }
            Section {
                if model.devices.isEmpty && model.didLoad && model.error == nil {
                    Text("No devices.").foregroundStyle(.secondary)
                }
                ForEach(model.devices) { device in
                    DeviceRow(device: device)
                        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                            if AccountDevicesModel.canRevoke(device) {
                                Button("Revoke", role: .destructive) { Task { await model.revoke(device) } }
                            }
                        }
                }
            }
            if model.offersSignOutEverywhere {
                Section {
                    Button("Sign out everywhere", role: .destructive) { confirmingSignOut = true }
                        .disabled(model.busy)
                } footer: {
                    if let notice = model.notice { Text(notice) }
                }
            }
        }
        .navigationTitle("Devices")
        .navigationBarTitleDisplayMode(.inline)
        .overlay {
            if !model.didLoad { ProgressView() }
        }
        .refreshable { await model.load() }
        .task { await model.load() }
        .confirmationDialog("Sign out everywhere", isPresented: $confirmingSignOut, titleVisibility: .hidden) {
            Button("Sign out everywhere", role: .destructive) { Task { await model.signOutEverywhere() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(AccountDevicesModel.signOutEverywhereMessage)
        }
    }
}

private struct DeviceRow: View {
    let device: AccountDevice

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(AccountDevicesModel.oneLine(device.name)).lineLimit(1)
            Text(AccountDevicesModel.subtitle(for: device)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .accessibilityElement(children: .combine)
    }
}
