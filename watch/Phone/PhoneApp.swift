import SwiftUI
import WristcallKit

@main
struct PhoneApp: App {
    @State private var state = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(state)
                .task { await start() }
        }
    }

    private func start() async {
        // The unit tests run hosted in this app: they bring their own state and nothing should hit the network.
        guard ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] == nil else { return }
        await state.load()
        #if DEBUG
        await DebugLaunch.apply(to: state)
        #endif
    }
}

struct RootView: View {
    var body: some View {
        TabView {
            Tab("Servers", systemImage: "server.rack") {
                ServersView()
            }
            Tab("History", systemImage: "clock") {
                NavigationStack {
                    ContentUnavailableView(
                        "No history yet", systemImage: "clock",
                        description: Text("Calls from your servers will show up here."))
                    .navigationTitle("History")
                }
            }
            Tab("Settings", systemImage: "gear") {
                SettingsView()
            }
        }
    }
}

struct SettingsView: View {
    /// Placeholder until the privacy policy has a home (store debt).
    static let privacyURL = URL(string: "https://github.com/ggondim/wristcall#privacy")!

    var body: some View {
        NavigationStack {
            Form {
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
/// `-debugOpen server|agents|form-new|form-edit` opens that screen at launch (simulator smoke tests have no
/// way to tap): the first server, its agents, the form for a new agent, or the form of the last agent.
enum DebugRoute {
    static let value: String? = {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "-debugOpen"), args.indices.contains(index + 1) else { return nil }
        return args[index + 1]
    }()

    static var opensServer: Bool { value != nil }
    static var opensAgents: Bool { ["agents", "form-new", "form-edit"].contains(value ?? "") }
}
#endif
