import SwiftUI
import WristcallKit

/// "Use on the web" waits for the web app's sign-in (O7, decision C13): the row only shows when this is true.
let WEB_SIGN_IN_READY = false

/// Where the Settings stack can go. Server details are routes here (not in `ServersView`) so that a tapped
/// approval notification can open one from the Settings tab.
enum SettingsRoute: Hashable {
    case servers
    case devices
    case server(String)

    /// Where a tapped approval notification goes: the servers list, then the server (its devices open there).
    static func path(opening serverID: String) -> [SettingsRoute] { [.servers, .server(serverID)] }
}

/// What Settings says, apart from how it looks (the views only draw this).
enum SettingsContent {
    struct PlanRow: Equatable {
        var title: String
        var detail: String
    }

    enum AccountRow: Equatable {
        case useOnTheWeb, devices
    }

    /// The top row: the plan of the account (Cloud, or Cloud with the user's models), or the way to choose one.
    static func planRow(for plan: Plan?) -> PlanRow {
        guard let plan else { return PlanRow(title: "No plan", detail: "Choose how to start") }
        let title = plan.plan == .custom ? PlanOption.cloudCustom.title : PlanOption.cloud.title
        return PlanRow(title: title, detail: "Beta · \(sourceText(plan.source))")
    }

    /// The Cloud writes `beta` for every redeemed invite (and `invite` for older ones): both read "invite".
    /// Any other source prints as is.
    static func sourceText(_ source: String) -> String {
        ["beta", "invite"].contains(source) ? "invite" : source
    }

    /// ACCOUNT section rows. "Use on the web" only once the web sign-in exists.
    static func accountRows(webSignInReady: Bool = WEB_SIGN_IN_READY) -> [AccountRow] {
        webSignInReady ? [.useOnTheWeb, .devices] : [.devices]
    }

    static let webDetail = "Scan the code at wristcall-app.trigram.com.br"
    static let deleteTitle = "Delete account"
    static let deleteExplanation = "Delete your wristcall account? This deletes your account, the agents list in "
        + "Wristcall Cloud and every signed-in device. Servers you run keep their data."

    /// Placeholder until the privacy policy has a home (store debt).
    static let privacyURL = URL(string: "https://github.com/ggondim/wristcall#privacy")!

    static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(version) (\(build))"
    }
}

/// The Settings tab (mockup `iphone-08`, without Apple sign-in): plan, account, advanced, privacy.
struct SettingsView: View {
    /// The plan row: the root shows the plan screen in a sheet.
    let onChoosePlan: () -> Void

    init(account: AccountModel, onChoosePlan: @escaping () -> Void) {
        self.onChoosePlan = onChoosePlan
        _devices = State(initialValue: AccountDevicesModel(account: account))
    }

    @Environment(AppState.self) private var state
    @Environment(AccountModel.self) private var account
    @Environment(ApprovalsModel.self) private var approvals
    @Environment(LinkRouter.self) private var router
    @Environment(\.openURL) private var openURL
    @State private var path: [SettingsRoute] = []
    @State private var devices: AccountDevicesModel
    @State private var confirmingDelete = false
    @State private var busy = false

    private var hasAccount: Bool { account.state != .unavailable }
    private var isReady: Bool { account.state == .ready }

    var body: some View {
        NavigationStack(path: $path) {
            List {
                statusSection
                if hasAccount { planSection }
                if hasAccount { accountSection }
                advancedSection
                dangerSection
            }
            // The floating tab bar covers the bottom of the list: leave room so the version can scroll above it.
            .contentMargins(.bottom, 24, for: .scrollContent)
            .navigationTitle("Settings")
            .navigationDestination(for: SettingsRoute.self) { route in
                switch route {
                case .servers: ServersView()
                case .devices:
                    AccountDevicesView(model: devices)
                case .server(let id): ServerDetailView(serverID: id)
                }
            }
        }
        .task(id: account.state) {
            if isReady { await devices.load() }
        }
        // A tapped notification: the server of its tag, among the saved ones only (the list may still be
        // loading on a cold start, hence the servers in the key). Its detail screen opens the devices.
        .task(id: OpenKey(request: approvals.openRequest, servers: state.servers.map(\.id))) {
            guard let request = approvals.openRequest else { return }
            guard let id = request.serverID, state.servers.contains(where: { $0.id == id }) else {
                // Nothing to open but the tab (unless the list is not read yet).
                if !state.servers.isEmpty || state.loadError != nil { approvals.openRequest = nil }
                return
            }
            let target = SettingsRoute.path(opening: id)
            if path != target { path = target }
        }
        #if DEBUG
        .task(id: state.servers.first?.id) {
            if DebugRoute.opensServer, let id = state.servers.first?.id, path.isEmpty { path = SettingsRoute.path(opening: id) }
        }
        #endif
    }

    // MARK: - Sections

    /// The account is not ready (or failed), or the last action said something.
    @ViewBuilder
    private var statusSection: some View {
        if let note = PlanModel.blocker(for: account.state), hasAccount {
            Section {
                if case .starting = account.state {
                    HStack {
                        Text(note)
                        Spacer()
                        ProgressView()
                    }
                } else if case .failed = account.state {
                    Text(note).foregroundStyle(.red)
                    Button("Try again") { Task { await account.start() } }
                } else {
                    Text(note).foregroundStyle(.secondary)
                }
            }
        } else if isReady, account.agenda?.notice != nil || account.error != nil {
            Section {
                if let notice = account.agenda?.notice {
                    Text(notice).font(.footnote).foregroundStyle(.orange)
                }
                if let error = account.error {
                    Text(error).foregroundStyle(.red)
                }
            }
        }
    }

    private var planSection: some View {
        let row = SettingsContent.planRow(for: account.plan)
        return Section {
            Button(action: onChoosePlan) {
                SettingsRowLabel(symbol: "cloud.fill", tint: .green, title: row.title, subtitle: row.detail, chevron: true)
            }
            .buttonStyle(.plain)
        }
    }

    private var accountSection: some View {
        Section {
            ForEach(SettingsContent.accountRows(), id: \.self) { row in
                switch row {
                case .useOnTheWeb:
                    Button { router.sheet = .scanner(purpose: .web) } label: {
                        SettingsRowLabel(
                            symbol: "globe", tint: .teal, title: "Use on the web",
                            subtitle: SettingsContent.webDetail, chevron: true)
                    }
                    .buttonStyle(.plain)
                    .disabled(!isReady)
                case .devices:
                    NavigationLink(value: SettingsRoute.devices) {
                        SettingsRowLabel(
                            symbol: "applewatch", tint: .gray, title: "Devices",
                            value: devices.didLoad ? String(devices.devices.count) : nil)
                    }
                    .disabled(!isReady)
                }
            }
            if account.isMigratedAccount {
                NavigationLink("Approve watch sign-in") { ApproveWatchSignInView() }
            }
        } header: {
            Text("Account")
        }
    }

    private var advancedSection: some View {
        Section {
            NavigationLink(value: SettingsRoute.servers) {
                SettingsRowLabel(
                    symbol: "server.rack", tint: .black, title: "Servers", value: String(state.servers.count),
                    alert: approvals.pending.isEmpty ? nil : "\(approvals.pending.count) waiting")
            }
            Button { router.sheet = .scanner(purpose: .server) } label: {
                SettingsRowLabel(symbol: "qrcode.viewfinder", tint: .blue, title: "Scan QR code", chevron: true)
            }
            .buttonStyle(.plain)
        } header: {
            Text("Advanced")
        }
    }

    private var dangerSection: some View {
        Section {
            Button { openURL(SettingsContent.privacyURL) } label: {
                SettingsRowLabel(symbol: "shield", tint: .gray, title: "Privacy", chevron: true)
            }
            .buttonStyle(.plain)
            if hasAccount {
                Button(role: .destructive) { confirmingDelete = true } label: {
                    SettingsRowLabel(symbol: "trash", tint: .red, title: "Delete account", titleColor: .red)
                }
                .buttonStyle(.plain)
                .disabled(busy || !isReady)
                .confirmationDialog("Delete account?", isPresented: $confirmingDelete, titleVisibility: .visible) {
                    Button("Delete account", role: .destructive) {
                        busy = true
                        Task {
                            await account.deleteAccount()
                            busy = false
                        }
                    }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text(SettingsContent.deleteExplanation)
                }
            }
        } footer: {
            Text("Version \(SettingsContent.versionText)").frame(maxWidth: .infinity)
        }
    }
}

private struct OpenKey: Equatable {
    var request: ApprovalsModel.OpenRequest?
    var servers: [String]
}

/// A Settings row: a colored symbol tile, a title with an optional second line, and what is on the right.
private struct SettingsRowLabel: View {
    let symbol: String
    let tint: Color
    let title: String
    var subtitle: String?
    var value: String?
    /// Something to look at on the right, in orange (approvals waiting).
    var alert: String?
    var titleColor: Color = .primary
    /// Rows that are not `NavigationLink`s draw their own chevron.
    var chevron = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(tint, in: .rect(cornerRadius: 8))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).foregroundStyle(titleColor)
                if let subtitle {
                    Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 8)
            if let alert { Text(alert).font(.subheadline).foregroundStyle(.orange) }
            if let value { Text(value).foregroundStyle(.secondary) }
            if chevron {
                Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
        }
        .contentShape(.rect)
    }
}
