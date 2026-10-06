import SwiftUI
import WristcallKit

@main
struct WristcallApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .task {
                    await model.launch()
                    #if DEBUG
                    DebugPairing.run(model, arguments: ProcessInfo.processInfo.arguments)
                    #endif
                }
        }
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
