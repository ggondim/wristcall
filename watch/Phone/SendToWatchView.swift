import SwiftUI
import WristcallKit

/// "Add to watch" (decision R10): asks the server for a new code and sends it to the paired Apple Watch,
/// which pairs as if the code had been typed. Without a watch, the code and address to type instead.
struct SendToWatchView: View {
    let server: ManagedServer
    @Environment(AppState.self) private var state
    @Environment(WatchLink.self) private var watchLink
    @State private var result: WatchLink.SendResult?
    @State private var sending = false
    /// The code to type, when there is no watch to send it to.
    @State private var codeModel: DevicesModel?

    var body: some View {
        Group {
            if let codeModel {
                PairingCodeView(model: codeModel)
            } else {
                Form { content }
            }
        }
        .navigationTitle("Add to watch")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if result == nil && !sending { await send() }
        }
    }

    @ViewBuilder
    private var content: some View {
        Section {
            if sending {
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Sending to the watch…")
                }
            } else if result != .paired && result != nil && watchLink.isOnWatch(server) {
                // Queued, pending or slow: the watch's context says it is done.
                Label("On watch", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                Text("\(server.name) is on the watch with its agents.")
            } else {
                switch result {
                case .paired:
                    Label("On watch", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    Text("\(server.name) is on the watch with its agents.")
                case .queued:
                    Label("Waiting for the watch", systemImage: "clock")
                    Text("The code goes to the watch when it is reachable: open Wristcall on it. The code works for 10 minutes.")
                        .foregroundStyle(.secondary)
                case .pending(let requestId):
                    Label("Approve the watch", systemImage: "hand.raised")
                    Text(requestId.map { "This server wants your approval: approve request \($0) in Devices. The watch waits for it." }
                         ?? "This server wants your approval: approve the watch in Devices. The watch waits for it.")
                        .foregroundStyle(.secondary)
                    NavigationLink {
                        DevicesView(server: server)
                    } label: {
                        Label("Devices", systemImage: "applewatch")
                    }
                case .stillPairing:
                    Label("Still pairing on the watch…", systemImage: "hourglass")
                    Text("The watch has the code. This shows On watch once it is done.")
                        .foregroundStyle(.secondary)
                case .failed(let text):
                    Text(WatchLink.text(forReason: text)).foregroundStyle(.red)
                    Button("Try again", systemImage: "arrow.clockwise") { Task { await send() } }
                case .unavailable, nil:
                    EmptyView()
                }
            }
        } footer: {
            Text("Only a pairing code goes to the watch, never a token. Each code works once.")
        }
        if !sending {
            Section {
                Button("Show the code instead", systemImage: "number.square") { showCode() }
            } footer: {
                Text("Type it on the watch: Use server URL, then the code.")
            }
        }
    }

    private func send() async {
        sending = true
        defer { sending = false }
        let outcome = await watchLink.send(server: server, api: state.api(for: server))
        result = outcome
        if outcome == .unavailable { showCode() }
    }

    /// `PairingCodeView` asks for a code itself when it opens; a code still queued for the watch goes.
    private func showCode() {
        watchLink.cancelQueuedPair()
        codeModel = DevicesModel(api: state.api(for: server))
    }
}

extension WatchLink.SendResult {
    /// One line for the pairing code screen.
    var summary: String {
        switch self {
        case .paired: "On watch."
        case .queued: "Waiting for the watch: open Wristcall on it."
        case .pending(let requestId):
            requestId.map { "Approve request \($0) in Devices: the watch waits for it." }
                ?? "Approve the watch in Devices: it waits for it."
        case .stillPairing: "Still pairing on the watch…"
        case .failed(let text): WatchLink.text(forReason: text)
        case .unavailable: "No Apple Watch with Wristcall is paired with this iPhone."
        }
    }

    var isFailure: Bool {
        switch self {
        case .failed, .unavailable: true
        case .paired, .queued, .pending, .stillPairing: false
        }
    }
}

/// The server's line in the Apple Watch section: "On watch" with "Refresh watch", or "Add to watch".
struct WatchSection: View {
    let server: ManagedServer
    @Environment(WatchLink.self) private var watchLink

    var body: some View {
        Section {
            if watchLink.isOnWatch(server) {
                Label("On watch", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                RefreshWatchButton(compact: false)
            } else {
                NavigationLink {
                    SendToWatchView(server: server)
                } label: {
                    Label("Add to watch", systemImage: "applewatch")
                }
            }
        } header: {
            Text("Apple Watch")
        } footer: {
            if !watchLink.isOnWatch(server) {
                Text(watchLink.canReachWatch
                     ? "Sends a pairing code to your Apple Watch, so you do not type it."
                     : "No Apple Watch with Wristcall is paired with this iPhone: you get a code to type on the watch.")
            }
        }
    }
}

/// "Refresh watch": the watch asks its servers for their agents again (decision R16).
struct RefreshWatchButton: View {
    /// A footer line ("On watch ✓ · Refresh watch") rather than a row.
    let compact: Bool
    @Environment(WatchLink.self) private var watchLink
    @State private var working = false
    @State private var note: String?

    var body: some View {
        if compact {
            HStack(spacing: 6) {
                Label("On watch", systemImage: "checkmark.circle.fill")
                Text("·")
                button.buttonStyle(.borderless)
                if let note { Text(note) }
            }
            .font(.footnote)
        } else {
            button
            if let note { Text(note).foregroundStyle(.secondary) }
        }
    }

    private var button: some View {
        Button("Refresh watch", systemImage: "arrow.clockwise") {
            Task {
                working = true
                defer { working = false }
                note = await watchLink.refreshWatch() ? "Sent." : "Open Wristcall on the watch, then try again."
            }
        }
        .disabled(working)
    }
}
