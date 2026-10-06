import Combine
import SwiftUI
import WristcallKit

@main
struct WristcallApp: App {
    @State private var model: AppModel
    /// Owned here for the app's lifetime; `AppModel.callHandler` points to it.
    private let coordinator: CallCoordinator
    /// Starts the calls that the App Intent asked for (phase 5).
    private let shortcuts: ShortcutCalls
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let model = AppModel()
        let coordinator = CallCoordinator(callControl: Self.makeCallControl(), audio: Self.makeAudio())
        coordinator.model = model
        model.callHandler = coordinator
        _model = State(initialValue: model)
        self.coordinator = coordinator
        shortcuts = ShortcutCalls(store: PendingCallStore(), model: model)
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .task {
                    await model.launch()
                    // On a cold start the intent may record its request before `onReceive`
                    // subscribes, with the scene already active: check once after launch.
                    await shortcuts.check()
                    #if DEBUG
                    DebugPairing.run(model, arguments: ProcessInfo.processInfo.arguments)
                    await DebugCall.run(model, arguments: ProcessInfo.processInfo.arguments)
                    #endif
                }
                // A shortcut may record its request before or after the app becomes active.
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active {
                        Task { await shortcuts.check() }
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: PendingCallStore.didRequest)) { _ in
                    Task { await shortcuts.check() }
                }
                // The complication opens `wristcall://call`.
                .onOpenURL { url in
                    guard ShortcutLink.isCall(url) else { return }
                    PendingCallStore().request()
                }
        }
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
/// Without `-pairCode` it sends an approval request (flow B). Ignored when already paired.
enum DebugPairing {
    @MainActor
    static func run(_ model: AppModel, arguments: [String]) {
        guard model.phase == .unpaired, let server = value(after: "-pairServer", in: arguments) else { return }
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
