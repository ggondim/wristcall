import os
import SwiftUI
import UIKit
import UserNotifications
import WatchConnectivity
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
    @State private var watchLink: WatchLink
    /// wristcall links (`onOpenURL`, the QR scanner): the sheets they open.
    @State private var router = LinkRouter()
    @Environment(\.scenePhase) private var scenePhase
    /// The watch login code whose approval sheet the user closed (it does not come back for that code).
    @State private var dismissedWatchCode: String?
    /// The launch read the servers: a start from the foreground may run.
    @State private var launched = false

    init() {
        let state = AppState()
        _state = State(initialValue: state)
        let approvals = ApprovalsModel(state: state)
        _approvals = State(initialValue: approvals)
        _history = State(initialValue: HistoryModel(state: state))
        // The Cloud comes from the build only (never from a server); empty: no account at all.
        let cloud = AccountModel.cloudURL(fromInfoValue: Bundle.main.object(forInfoDictionaryKey: "WristcallCloudURL") as? String)
        // The device session is the account; the 0.3.x Zitadel tokens are only read to move them over (and by
        // the email restore). The account keys live in the shared Keychain group (iCloud Keychain).
        let session = cloud.map { DeviceSession(cloud: $0, store: KeychainDeviceSessionStore()) }
        let legacy = cloud.map { AccountSession(cloud: $0, kind: .ios, store: KeychainTokenStore()) }
        let group = (Bundle.main.object(forInfoDictionaryKey: "WristcallKeychainGroup") as? String)
            .flatMap { $0.isEmpty || $0.contains("$(") ? nil : $0 }
        let account = AccountModel(
            cloudURL: cloud, session: session, keys: KeychainAccountKeyStore(accessGroup: group), legacy: legacy,
            web: LiveWebAuthenticator(), state: state,
            device: CloudDeviceInfo(name: String(UIDevice.current.name.prefix(64)), platform: .ios)
        )
        _account = State(initialValue: account)
        let watchLink = Self.makeWatchLink()
        _watchLink = State(initialValue: watchLink)
        // M13: the watch's login is done: the device page it was approved on closes.
        watchLink.onWatchSignedIn = { [weak account] in account?.watchSignInFinished() }
        // The watch app was installed after the account was ready: it gets its enroll code now.
        watchLink.onWatchAppInstalled = { [weak account] in account?.publishWatchCodeInBackground() }
        // C6: a watch in this account gets pairing codes for the `approval` servers it lacks.
        watchLink.onWatchContext = { [weak account] in account?.pairWatchWithMissingServersInBackground() }
        // A server linked by its QR code is set up on the watch too.
        account.watchLink = watchLink
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
                .environment(watchLink)
                .environment(router)
                .onOpenURL { router.open($0) }
                // R11: the watch sent its login code and the account is signed in here: approve it.
                .sheet(item: Binding(
                    get: {
                        WatchSignInSheet.code(link: watchLink, account: account, dismissed: dismissedWatchCode)
                            .map(WatchSignInCode.init)
                    },
                    set: { value in
                        if value == nil { dismissedWatchCode = watchLink.incomingDeviceCode ?? dismissedWatchCode }
                    }
                )) { code in
                    NavigationStack {
                        ApproveWatchSignInView(code: code.value, isSheet: true)
                    }
                    .closesForLinks()
                    .environment(account)
                    // Outside `RootView`: the router reaches this sheet only through here.
                    .environment(router)
                }
                .task { await start() }
                .onChange(of: scenePhase) { _, phase in
                    // Back in the foreground: ask the servers for device approvals waiting for the owner.
                    guard phase == .active, !Self.isUnitTest else { return }
                    Task { await approvals.refresh() }
                    // The account: a session revoked meanwhile, a key that arrived late, a wait that is over. Not
                    // before the launch's own start, which reads the servers first (the mirror needs them).
                    if launched { Task { await account.start() } }
                    // I2: a queued pairing code that expired while the app was away does not reach the watch.
                    watchLink.sweepExpiredTransfers()
                    #if WRISTCALL_PUSH
                    // R20: every activation checks the push keys again.
                    if let push = PhoneAppDelegate.push { Task { await push.sync() } }
                    #endif
                }
        }
    }

    /// WatchConnectivity with the watch app (decision R10). Unit tests (hosted in this app) and devices
    /// without it get a link that reports no watch.
    @MainActor
    private static func makeWatchLink() -> WatchLink {
        guard !isUnitTest, WCSession.isSupported() else { return WatchLink(session: nil) }
        let session = LiveWatchSession()
        let link = WatchLink(session: session)
        session.attach(link)
        session.activate()
        return link
    }

    static var isUnitTest: Bool { ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil }

    private func start() async {
        // The unit tests run hosted in this app: they bring their own state and nothing should hit the network.
        guard !Self.isUnitTest else { return }
        #if DEBUG
        DebugLaunch.prepare()
        #endif
        await state.load()
        #if DEBUG
        await DebugLaunch.apply(to: state)
        if let url = DebugLaunch.openURL { router.open(url) }
        if DebugRoute.opensScanner { router.sheet = .scanner(purpose: .server) }
        #endif
        await approvals.refresh()
        launched = true
        await account.start()
    }
}

/// What can make someone count as already in use: the servers were read, or one was added.
private struct FinishKey: Equatable {
    var loaded: Bool
    var hasServers: Bool
}

struct RootView: View {
    @Environment(AppState.self) private var state
    @Environment(AccountModel.self) private var account
    @Environment(ApprovalsModel.self) private var approvals
    @Environment(LinkRouter.self) private var router
    @State private var tab = Self.firstTab
    @State private var step: OnboardingStep = Self.firstStep
    /// Read from the defaults, so finishing the onboarding (or an account deletion resetting it) redraws.
    @AppStorage(OnboardingStore.key) private var onboardingDone = false
    /// "Try Wristcall Cloud" (Agents home): the plan screen in a sheet.
    @State private var choosingPlan = false
    /// The plan sheet ended on "Your own server": the scanner opens once the sheet is gone.
    @State private var planWantsServer = false

    private static var firstTab: String {
        #if DEBUG
        if DebugRoute.opensHistory { return "history" }
        if DebugRoute.opensSettings || DebugRoute.opensServer { return "settings" }
        #endif
        return "agents"
    }

    private static var firstStep: OnboardingStep {
        #if DEBUG
        if DebugRoute.opensPlan { return .plan }
        #endif
        return .welcome
    }

    /// Onboarding not gone through and the servers not read yet (nor failed to read).
    private var isDeciding: Bool {
        !onboardingDone && !state.didLoad && state.loadError == nil
    }

    private var entryStep: OnboardingStep? {
        OnboardingGate.entryStep(
            isDone: onboardingDone, serversLoaded: state.didLoad,
            hasServers: !state.servers.isEmpty, didMigrate: account.didMigrate
        )
    }

    var body: some View {
        Group {
            if isDeciding {
                // Not known yet whether the welcome shows: a blank screen, not a frame of the empty tabs.
                Color(.systemBackground).ignoresSafeArea()
            } else if entryStep != nil {
                onboarding
            } else {
                tabs
            }
        }
        // Someone who already uses the app never sees the welcome: it counts as gone through.
        .task(id: FinishKey(loaded: state.didLoad, hasServers: !state.servers.isEmpty)) { finishIfAlreadyInUse() }
        .onChange(of: account.didMigrate) { finishIfAlreadyInUse() }
        // After an account deletion the onboarding starts over from the welcome.
        .onChange(of: onboardingDone) { if !onboardingDone { step = .welcome } }
        // No way into the account: sign in again (or start over). Not dismissible by swiping.
        .sheet(isPresented: Binding(
            get: { OnboardingGate.showsRestore(for: account.state) },
            set: { _ in }
        )) {
            RestoreView(account: account, allowsStartOver: true) { OnboardingStore.finish() }
                // Never closed for a link: the link waits until the account is back (the sheet goes on its own).
                .closesForLinks(keepsOpen: true)
        }
        .modifier(LinkRouterPresenter())
        .sheet(isPresented: $choosingPlan, onDismiss: {
            if planWantsServer {
                planWantsServer = false
                router.sheet = .scanner(purpose: .server)
            }
        }) {
            NavigationStack {
                PlanView(mode: .settings) { choice in
                    if case .ownServer = choice { planWantsServer = true }
                    choosingPlan = false
                }
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Close") { choosingPlan = false } }
                }
            }
            .closesForLinks()
        }
    }

    private func finishIfAlreadyInUse() {
        if OnboardingGate.shouldFinishSilently(
            isDone: onboardingDone, serversLoaded: state.didLoad,
            hasServers: !state.servers.isEmpty, didMigrate: account.didMigrate
        ) {
            OnboardingStore.finish()
        }
    }

    @ViewBuilder
    private var onboarding: some View {
        switch step {
        case .welcome:
            WelcomeView(
                onContinue: { withAnimation { step = .plan } },
                onRestored: { OnboardingStore.finish() }
            )
            .transition(.opacity)
        case .plan:
            PlanView(mode: .onboarding) { choice in
                tab = "agents"
                if OnboardingGate.finish(with: choice) { router.sheet = .scanner(purpose: .server) }
            }
            .transition(.opacity)
        }
    }

    private var tabs: some View {
        TabView(selection: $tab) {
            Tab("Agents", systemImage: "person.2.wave.2", value: "agents") {
                AgentsHomeView(onScan: { router.sheet = .scanner(purpose: .server) }, onTryCloud: { choosingPlan = true })
            }
            Tab("History", systemImage: "clock", value: "history") {
                HistoryView()
            }
            Tab("Settings", systemImage: "gear", value: "settings") {
                SettingsView(account: account, onChoosePlan: { choosingPlan = true })
            }
            // Device approvals waiting for the owner (they are answered under Servers).
            .badge(approvals.pending.count)
        }
        // A tapped notification opens the Settings tab (Servers, then the server's devices).
        .task(id: approvals.openRequest) {
            if approvals.openRequest != nil { tab = "settings" }
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

#if DEBUG
/// Launch arguments for the simulator (Debug builds only), like the watch app's: `-addServer <URL>
/// -addServerToken <wc_pat_…>` adds a server at launch.
enum DebugLaunch {
    /// Before the servers are read: `-resetOnboarding` brings the welcome screen back.
    static func prepare() {
        if ProcessInfo.processInfo.arguments.contains("-resetOnboarding") { OnboardingStore.reset() }
    }

    /// `-debugOpenURL <url>`: the URL goes through the link router at launch, as if opened from outside.
    static var openURL: URL? {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "-debugOpenURL"), args.indices.contains(index + 1) else { return nil }
        return URL(string: args[index + 1])
    }

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
/// Settings tab, `-debugOpen plan` the plan screen of the onboarding (with `-resetOnboarding`); `-debugOpen server|agents|form-new|form-edit|devices|code|watch` opens that screen at launch (a way to reach a screen
/// without tapping, for screenshots from the command line): the first server, its agents, the form for a new agent, the form of the last agent, the devices of the server, its
/// pairing code, or "Add to watch" (which sends a new code to the paired watch at once). `-debugOpen newAgent|editAgent` opens the agent editor (new, or the last agent of the first server) on the Agents tab. `-debugOpen scanner`
/// opens the QR scanner.
enum DebugRoute {
    static let value: String? = {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "-debugOpen"), args.indices.contains(index + 1) else { return nil }
        return args[index + 1]
    }()

    static var opensHistory: Bool { ["history", "history-detail"].contains(value ?? "") }
    static var opensSettings: Bool { value == "settings" }
    static var opensPlan: Bool { value == "plan" }
    static var opensServer: Bool {
        value != nil && !opensHistory && !opensSettings && !opensPlan && !opensEditor && !opensScanner
    }
    /// `-debugOpen scanner`: the QR scanner (server mode).
    static var opensScanner: Bool { value == "scanner" }
    static var opensDevices: Bool { ["devices", "code"].contains(value ?? "") }
    static var opensAgents: Bool { ["agents", "form-new", "form-edit"].contains(value ?? "") }
    static var opensEditor: Bool { ["newAgent", "editAgent"].contains(value ?? "") }
    static var opensSendToWatch: Bool { value == "watch" }
}
#endif
