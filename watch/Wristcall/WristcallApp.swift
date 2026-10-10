import Combine
import Intents
import SwiftUI
import UserNotifications
import WatchKit
import WidgetKit
import WristcallKit

@main
struct WristcallApp: App {
    @State private var model: AppModel
    /// Owned here for the app's lifetime; `AppModel.callHandler` points to it.
    private let coordinator: CallCoordinator
    /// Starts the calls that the App Intent asked for (phase 5).
    private let shortcuts: ShortcutCalls
    /// Push notifications for one-way results (task 12): the push build only (`make build-sim-push`).
    private let push: PushCoordinator?
    /// WatchConnectivity with the iPhone app (decision R10): pairing it sends, "Refresh watch", and
    /// the servers this watch is paired with.
    private let link: WatchLinkReceiver
    /// The login with the central account (decision R11); unavailable when the build has no Cloud.
    @State private var login: AccountLoginModel
    #if WRISTCALL_PUSH
    @WKApplicationDelegateAdaptor private var appDelegate: AppDelegate
    #endif
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // The path monitor starts here, at launch, so it has an answer before the first "Call".
        // The last run's catalog keeps the agents of a server that does not answer this time.
        let model = AppModel(reachability: NetworkPathMonitor(), savedCatalog: AgentCatalog.shared().load())
        let coordinator = CallCoordinator(callControl: Self.makeCallControl(), audio: Self.makeAudio())
        coordinator.model = model
        model.callHandler = coordinator
        // The result of a one-way call may arrive with the wrist down: a tap says how it went.
        model.onCallResultFinished = { delivered in
            WKInterfaceDevice.current().play(delivered ? .success : .failure)
        }
        // Told only when the catalog really changed: the widget extension reads it from the App
        // Group to configure the complication and the control, and Shortcuts lists its agents.
        model.onAgentsChanged = { catalog in
            AgentCatalog.shared().save(catalog)
            WidgetCenter.shared.reloadAllTimelines()
            ControlCenter.shared.reloadAllControls()
            WristcallShortcuts.updateAppShortcutParameters()
        }
        _model = State(initialValue: model)
        self.coordinator = coordinator
        shortcuts = ShortcutCalls(store: PendingCallStore(), model: model)
        push = Self.makePush(model)
        let link = WatchLinkReceiver(model: model)
        self.link = link
        // The Cloud comes from the build only (never from a server or the iPhone); empty: no account login.
        let cloud = AccountLoginModel.cloudURL(
            fromInfoValue: Bundle.main.object(forInfoDictionaryKey: "WristcallCloudURL") as? String)
        let session = cloud.map {
            AccountSession(cloud: $0, kind: .watch, store: KeychainTokenStore(service: KeychainTokenStore.defaultService))
        }
        _login = State(initialValue: AccountLoginModel(
            cloudURL: cloud, session: session, model: model, sendToPhone: { [weak link] in link?.send($0) }))
        // Unit tests run inside this app: they talk to their own receiver, never to WatchConnectivity.
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil {
            link.activate()
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .environment(login)
                .task {
                    // A message from the iPhone may have started it already (WatchLinkReceiver).
                    await model.launchIfNeeded()
                    await login.restore()
                    // Once the servers are read: every one of them gets its push key.
                    push?.start()
                    // On a cold start the intent may record its request before `onReceive`
                    // subscribes, with the scene already active: check once after launch.
                    await shortcuts.check()
                    #if DEBUG
                    DebugPairing.run(model, arguments: ProcessInfo.processInfo.arguments)
                    await DebugCall.run(model, arguments: ProcessInfo.processInfo.arguments)
                    DebugShortcut.run(arguments: ProcessInfo.processInfo.arguments)
                    #endif
                }
                // A shortcut may record its request before or after the app becomes active.
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        // A result still waiting asks again (decision W10).
                        model.sceneDidBecomeActive()
                        push?.sceneDidBecomeActive()
                        Task { await shortcuts.check() }
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: PendingCallStore.didRequest)) { _ in
                    Task { await shortcuts.check() }
                }
                // The complications open `wristcall://call`, with the agent when one was chosen;
                // `wristcall://open` (no agent chosen yet) only brings the app up.
                .onOpenURL { url in
                    ShortcutCalls.request(from: url, store: PendingCallStore())
                }
                // Redial on the system call UI: the agent whose name is the CallKit handle.
                .onContinueUserActivity(Self.startCallActivity) { activity in
                    ShortcutCalls.requestRedial(of: activity.interaction?.intent, store: PendingCallStore())
                }
                .onContinueUserActivity(Self.startAudioCallActivity) { activity in
                    ShortcutCalls.requestRedial(of: activity.interaction?.intent, store: PendingCallStore())
                }
        }
    }

    /// The `NSUserActivity` types of the system's redial: `INStartCallIntent`, or the older
    /// `INStartAudioCallIntent` for an app that declares no calling intents. Receiving them needs
    /// no Intents entitlement.
    private static let startCallActivity = "INStartCallIntent"
    private static let startAudioCallActivity = "INStartAudioCallIntent"

    /// The push build's coordinator, the notification delegate from before launch ends (a tapped
    /// notification may be what launches the app). `nil` in the default builds and without a relay.
    @MainActor
    private static func makePush(_ model: AppModel) -> PushCoordinator? {
        #if WRISTCALL_PUSH
        let push = PushCoordinator()
        guard push.isEnabled else { return nil }
        push.model = model
        model.pushHandler = push
        UNUserNotificationCenter.current().delegate = push
        AppDelegate.push = push
        return push
        #else
        return nil
        #endif
    }

    @MainActor
    private static func makeCallControl() -> any CallControlling {
        #if DEBUG
        if !DebugCall.usesCallKit(ProcessInfo.processInfo.arguments) {
            return DirectAudioCallControl()
        }
        #endif
        return CallController()
    }

    @MainActor
    private static func makeAudio() -> any CallAudio {
        let audio = AudioIO()
        #if DEBUG
        audio.useSyntheticMic = DebugCall.usesSyntheticMic(ProcessInfo.processInfo.arguments)
        #endif
        return audio
    }
}

#if DEBUG
/// Simulator shortcut (Debug builds only), because typing on the simulated watch is slow:
///
///     xcrun simctl launch <device> <bundle id> -pairServer http://127.0.0.1:8765 -pairCode 12345678
///
/// does what "Use server URL" + the keypad + "Pair" do, through the same `AppModel` calls.
/// Without `-pairCode` it sends an approval request (flow B). When already paired it adds the
/// server (as "Add server" does), unless that server is already listed.
enum DebugPairing {
    @MainActor
    static func run(_ model: AppModel, arguments: [String]) {
        // `-addServer` alone opens the pairing screen over the servers already paired.
        if arguments.contains("-addServer"), model.phase == .home { model.addServer() }
        guard let server = value(after: "-pairServer", in: arguments) else { return }
        switch model.phase {
        case .unpaired: break
        case .home:
            guard let url = ServerAddress.parse(server), !model.servers.contains(where: { $0.credentials.serverURL == url })
            else { return }
            model.addServer()
        default: return
        }
        guard model.useServerURL(server) else { return }
        if let code = value(after: "-pairCode", in: arguments).flatMap(PairingCode.init) {
            model.pair(code: code)
        } else {
            model.requestApproval()
        }
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}
#endif
