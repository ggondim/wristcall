import SwiftUI
import WristcallKit

/// Home: the agents of every server in a two-column grid (a tap calls with the agent's own mode, a
/// long press opens its call options), a row for each server still loading or down, and the last
/// message. The "…" call options of 0.1.0 became that long press.
struct HomeView: View {
    let model: AppModel
    /// The agent whose call options are open.
    @State private var optionsTarget: AgentTarget?

    private let columns = [GridItem(.flexible(), spacing: 6), GridItem(.flexible(), spacing: 6)]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(model.servers.filter { !$0.isReady }) { entry in
                        ServerStatusRow(model: model, entry: entry)
                    }
                    if model.phase == .unavailable {
                        Button("Retry") {
                            Task { await model.retry() }
                        }
                        .frame(minHeight: 44)
                    }
                    LazyVGrid(columns: columns, spacing: 6) {
                        ForEach(model.agents) { target in
                            AgentCell(
                                target: target,
                                onTap: { model.startCall(target) },
                                onOptions: { open(target) })
                        }
                    }
                    if showsNoAgents {
                        Text("No agents on your servers.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    if let message = model.message {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                }
            }
            .navigationTitle("Wristcall")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink {
                        SettingsView(model: model)
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
            .navigationDestination(item: $optionsTarget) { target in
                CallOptionsView(model: model, target: target)
            }
            #if DEBUG
            .task {
                optionsTarget = await DebugCall.optionsTarget(model, arguments: ProcessInfo.processInfo.arguments)
            }
            #endif
        }
    }

    /// At least one server answered and none of them has an agent (a server that is down or still
    /// loading says so on its own row).
    private var showsNoAgents: Bool {
        model.phase == .home && model.agents.isEmpty && !model.isLoadingServers
            && model.servers.contains(where: \.isReady)
    }

    /// An agent this build cannot call has no options: the long press says why, like a tap.
    private func open(_ target: AgentTarget) {
        if target.agent.callType.isSupported {
            optionsTarget = target
        } else {
            model.startCall(target)
        }
    }
}

/// One agent: its icon in a circle and its name (two lines at most). An agent of a `call_type` this
/// build does not know is dimmed; tapping it says to update the app (decision W7).
private struct AgentCell: View {
    let target: AgentTarget
    let onTap: () -> Void
    let onOptions: () -> Void

    var body: some View {
        VStack(spacing: 4) {
            AgentIconView(icon: target.agent.icon)
                .font(.title3)
                .foregroundStyle(tint)
                .frame(width: 44, height: 44)
                .background(tint.opacity(0.25), in: Circle())
            Text(target.agent.displayName)
                .font(.caption2)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, minHeight: 44)
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .opacity(target.agent.callType.isSupported ? 1 : 0.4)
        // Not a `Button`: its tap would swallow the long press.
        .onTapGesture(perform: onTap)
        .onLongPressGesture(perform: onOptions)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(target.agent.displayName)
        .accessibilityValue(target.serverHost)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction(.default, onTap)
        .accessibilityAction(named: "Options", onOptions)
    }

    private var tint: Color {
        target.agent.callType.isOneWay ? .blue : .green
    }
}

/// A server that has not answered: "Loading <host>…", or "Can't reach <host>" with "Retry".
private struct ServerStatusRow: View {
    let model: AppModel
    let entry: ServerEntry

    var body: some View {
        if entry.isUnavailable {
            Button {
                Task { await model.retry(serverID: entry.id) }
            } label: {
                VStack(spacing: 2) {
                    Text("Can't reach \(entry.host)")
                        .font(.footnote)
                        .lineLimit(2)
                        .minimumScaleFactor(0.8)
                        .multilineTextAlignment(.center)
                    Label("Retry", systemImage: "arrow.clockwise")
                        .font(.footnote.bold())
                }
                .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("Can't reach \(entry.host)")
            .accessibilityHint("Retry")
        } else {
            HStack(spacing: 6) {
                ProgressView()
                    .controlSize(.small)
                Text("Loading \(entry.host)…")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
            }
            .frame(maxWidth: .infinity, minHeight: 32)
        }
    }
}

/// The app's own call screen (behind the system call UI on the watch): who, what is happening, and
/// "End" (conversation) or "Send" (one-way: hanging up is what sends the recording, decision W9).
/// Mute lives in the system call UI.
struct InCallView: View {
    let model: AppModel
    let target: AgentTarget

    var body: some View {
        VStack(spacing: 6) {
            AgentIconView(icon: target.agent.icon)
                .font(.title3)
                .foregroundStyle(target.agent.callType.isOneWay ? Color.blue : Color.green)
            Text(target.agent.displayName)
                .font(.title3)
                .lineLimit(2)
                .minimumScaleFactor(0.7)
                .multilineTextAlignment(.center)
            Text(model.callActivity.label)
                .font(.footnote)
                .foregroundStyle(.secondary)
            if target.agent.callType.isOneWay {
                Button {
                    model.endCall()
                } label: {
                    Label("Send", systemImage: "paperplane.fill")
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(.blue)
            } else {
                Button(role: .destructive) {
                    model.endCall()
                } label: {
                    Label("End", systemImage: "phone.down.fill")
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
            }
        }
    }
}
