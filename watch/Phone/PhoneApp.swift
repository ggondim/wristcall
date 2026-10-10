import os
import SwiftUI
import UIKit
import UserNotifications
import WristcallKit

@main
struct PhoneApp: App {
    /// The notification center's delegate is set at launch (a notification action may be what launched
    /// the app), and the APNs token arrives there.
    @UIApplicationDelegateAdaptor(PhoneAppDelegate.self) private var appDelegate
    @State private var state: AppState
    @State private var approvals: ApprovalsModel
    @State private var history: HistoryModel
    @State private var account: AccountModel
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let state = AppState()
        _state = State(initialValue: state)
        let approvals = ApprovalsModel(state: state)
        _approvals = State(initialValue: approvals)
        _history = State(initialValue: HistoryModel(state: state))
        // The Cloud comes from the build only (never from a server); empty: no account at all.
        let cloud = AccountModel.cloudURL(fromInfoValue: Bundle.main.object(forInfoDictionaryKey: "WristcallCloudURL") as? String)
        let session = cloud.map { AccountSession(cloud: $0, kind: .ios, store: KeychainTokenStore()) }
        let account = AccountModel(cloudURL: cloud, session: session, web: LiveWebAuthenticator(), state: state)
        _account = State(initialValue: account)
        // Read by the app delegate in `didFinishLaunching`, which runs after this.
        PhoneAppDelegate.notifications = ApprovalNotificationHandler(approvals: approvals)
        #if WRISTCALL_PUSH
        if !Self.isUnitTest, let push = PhonePushCoordinator(state: state) {
            push.install()
            // R18: deleting the account also undoes this iPhone's push keys.
            let deleted = account.accountDeleted
            account.accountDeleted = { [weak push] in
                await deleted?()
                await push?.forgetAll()
            }
            PhoneAppDelegate.push = push
        }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(state)
                .environment(approvals)
                .environment(history)
                .environment(account)
                .task { await start() }
                .onChange(of: scenePhase) { _, phase in
                    // Back in the foreground: ask the servers for device approvals waiting for the owner.
                    guard phase == .active, !Self.isUnitTest else { return }
                    Task { await approvals.refresh() }
                    #if WRISTCALL_PUSH
                    // R20: every activation checks the push keys again.
                    if let push = PhoneAppDelegate.push { Task { await push.sync() } }
                    #endif
                }
        }
    }

    fileprivate static var isUnitTest: Bool { ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil }

    private func start() async {
        // The unit tests run hosted in this app: they bring their own state and nothing should hit the network.
        guard !Self.isUnitTest else { return }
        await state.load()
        #if DEBUG
        await DebugLaunch.apply(to: state)
        #endif
        await approvals.refresh()
        await account.restore()
    }
}

struct RootView: View {
    @Environment(ApprovalsModel.self) private var approvals
    @State private var tab = Self.firstTab

    private static var firstTab: String {
        #if DEBUG
        if DebugRoute.opensHistory { return "history" }
        if DebugRoute.opensSettings { return "settings" }
        #endif
        return "servers"
    }

    var body: some View {
        TabView(selection: $tab) {
            Tab("Servers", systemImage: "server.rack", value: "servers") {
                ServersView()
            }
            .badge(approvals.pending.count)
            Tab("History", systemImage: "clock", value: "history") {
                HistoryView()
            }
            Tab("Settings", systemImage: "gear", value: "settings") {
                SettingsView()
            }
        }
        // A tapped notification opens the Servers tab (and there, the server's devices).
        .task(id: approvals.openRequest) {
            if approvals.openRequest != nil { tab = "servers" }
        }
    }
}

/// The UIKit callbacks SwiftUI has no equivalent for: the notification center's delegate, set before a
/// response that launched the app is delivered, and the APNs device token (push build).
final class PhoneAppDelegate: NSObject, UIApplicationDelegate {
    /// Set by `PhoneApp.init`, which runs before `didFinishLaunching`.
    @MainActor static var notifications: ApprovalNotificationHandler?
    #if WRISTCALL_PUSH
    @MainActor static var push: PhonePushCoordinator?
    #endif
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "push")

    func application(
        _ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let center = UNUserNotificationCenter.current()
        // Every build: the category is what gives a device approval its Approve and Deny.
        ApprovalNotifications.register(on: center)
        center.delegate = Self.notifications
        #if WRISTCALL_PUSH
        if !PhoneApp.isUnitTest { Self.push?.start() }
        #endif
        return true
    }

    #if WRISTCALL_PUSH
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        guard let push = Self.push else { return }
        Task { await push.didRegister(deviceToken: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        Self.log.error("push: APNs registration failed: \((error as NSError).code, privacy: .public)")
    }
    #endif
}

struct SettingsView: View {
    /// Placeholder until the privacy policy has a home (store debt).
    static let privacyURL = URL(string: "https://github.com/ggondim/wristcall#privacy")!

    var body: some View {
        NavigationStack {
            Form {
                AccountSection()
                Section {
                    LabeledContent("Version", value: Self.versionText)
                    Link("Privacy", destination: Self.privacyURL)
                }
            }
            .navigationTitle("Settings")
        }
    }

    static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(version) (\(build))"
    }
}

#if DEBUG
/// Launch arguments for the simulator (Debug builds only), like the watch app's: `-addServer <URL>
/// -addServerToken <wc_pat_…>` adds a server at launch.
enum DebugLaunch {
    @MainActor
    static func apply(to state: AppState) async {
        let args = ProcessInfo.processInfo.arguments
        func value(after flag: String) -> String? {
            guard let index = args.firstIndex(of: flag), args.indices.contains(index + 1) else { return nil }
            return args[index + 1]
        }
        guard let url = value(after: "-addServer"), let token = value(after: "-addServerToken") else { return }
        _ = try? await state.addServer(urlText: url, token: token, name: value(after: "-addServerName"))
    }
}
#endif

#if DEBUG
/// `-debugOpen history|history-detail` opens the History tab (or its first call), `-debugOpen settings` the
/// Settings tab; `-debugOpen server|agents|form-new|form-edit|devices|code` opens that screen at launch (simulator smoke tests have no
/// way to tap): the first server, its agents, the form for a new agent, the form of the last agent, the devices of the server, or its pairing code.
enum DebugRoute {
    static let value: String? = {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "-debugOpen"), args.indices.contains(index + 1) else { return nil }
        return args[index + 1]
    }()

    static var opensHistory: Bool { ["history", "history-detail"].contains(value ?? "") }
    static var opensSettings: Bool { value == "settings" }
    static var opensServer: Bool { value != nil && !opensHistory && !opensSettings }
    static var opensDevices: Bool { ["devices", "code"].contains(value ?? "") }
    static var opensAgents: Bool { ["agents", "form-new", "form-edit"].contains(value ?? "") }
}
#endif
