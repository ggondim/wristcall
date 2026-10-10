import SwiftUI
import WristcallKit

/// The 8 digit code that pairs a watch with a server: big digits, the server address to type on the
/// watch, and the time left. The code is a secret for 10 minutes and is used once.
struct PairingCodeView: View {
    let model: DevicesModel
    /// Hook of the "Add to watch" button (Task 10 sends the code to the paired watch). Without it the
    /// button is not shown and the screen is just the code and the address.
    var addToWatch: ((PairingCodeGrant) async -> Void)?

    @State private var working = false

    var body: some View {
        // The clock is read every second: the countdown moves and the code leaves the screen at expiry.
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            content(model.code)
        }
        .navigationTitle("Pairing code")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if model.code == nil { await generate() }
        }
    }

    private func content(_ grant: PairingCodeGrant?) -> some View {
        List {
            if let grant {
                Section {
                    VStack(spacing: 8) {
                        Text(Self.grouped(grant.code))
                            .font(.system(.largeTitle, design: .monospaced, weight: .semibold))
                            .minimumScaleFactor(0.5)
                            .lineLimit(1)
                            .privacySensitive()
                            .accessibilityLabel(grant.code.map(String.init).joined(separator: " "))
                            .textSelection(.enabled)
                        Text(Self.countdown(model.secondsLeft()))
                            .font(.title3.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("Expires in \(Self.countdown(model.secondsLeft()))")
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                } footer: {
                    Text("Type this code on the watch. It works once.")
                }

                Section("On the watch") {
                    LabeledContent("Server", value: grant.serverUrl)
                        .textSelection(.enabled)
                }

                if let warning = grant.warning, !warning.isEmpty {
                    Section {
                        Label(warning, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                    }
                }

                if let addToWatch {
                    Section {
                        Button("Add to watch", systemImage: "applewatch") {
                            Task {
                                working = true
                                defer { working = false }
                                await addToWatch(grant)
                            }
                        }
                        .disabled(working)
                    } footer: {
                        Text("Sends the code to your paired watch so you do not have to type it.")
                    }
                }
            } else if working {
                Section { ProgressView().frame(maxWidth: .infinity) }
            } else {
                Section {
                    ContentUnavailableView {
                        Label("No code", systemImage: "number")
                    } description: {
                        Text("A code lasts 10 minutes. Make a new one when you are ready to pair.")
                    }
                }
            }

            if let error = model.error {
                Section { Text(error).foregroundStyle(.red) }
            }

            Section {
                Button(grant == nil ? "Make a code" : "New code", systemImage: "arrow.clockwise") {
                    Task { await generate() }
                }
                .disabled(working)
            } footer: {
                if grant != nil { Text("A new code replaces this one.") }
            }
        }
    }

    private func generate() async {
        working = true
        defer { working = false }
        await model.newPairingCode()
    }

    /// `12345678` shown as `1234 5678`.
    static func grouped(_ code: String) -> String {
        guard code.count > 4 else { return code }
        let middle = code.index(code.startIndex, offsetBy: 4)
        return code[..<middle] + " " + code[middle...]
    }

    /// `m:ss`.
    static func countdown(_ seconds: Int) -> String {
        let seconds = max(0, seconds)
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}
