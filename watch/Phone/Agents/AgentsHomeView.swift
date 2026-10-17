import SwiftUI
import WristcallKit

/// The main tab (mockups `iphone-03` and `iphone-04`): the Apple Watch strip, the Cloud beta placeholder and one
/// section per server with its agents. Without a server it asks to connect one.
struct AgentsHomeView: View {
    /// "Scan QR code": the way to connect a server.
    let onScan: () -> Void
    /// "Try Wristcall Cloud".
    let onTryCloud: () -> Void

    /// What "How to run a server" opens: the README section.
    static let runServerURL = URL(string: "https://github.com/ggondim/wristcall#run-the-server-in-5-minutes")!
    static let cloudSoonMessage = "Wristcall Cloud agents arrive with the beta server. Your invite is saved."

    @Environment(AppState.self) private var state
    @State private var model: AllAgentsModel?

    var body: some View {
        NavigationStack {
            Group {
                if let model {
                    AgentsHomeContent(model: model, onScan: onScan, onTryCloud: onTryCloud)
                } else {
                    Color(.systemGroupedBackground)
                }
            }
            .navigationTitle("Agents")
        }
        .task {
            if model == nil { model = AllAgentsModel(state: state) }
        }
    }
}

/// An editor to show: a new agent, or one to edit, on its server.
private struct EditorSession: Identifiable {
    let id = UUID()
    let server: ManagedServer
    let editing: AgentDetail?
}

private struct AgentsHomeContent: View {
    let model: AllAgentsModel
    let onScan: () -> Void
    let onTryCloud: () -> Void

    @Environment(AppState.self) private var state
    @Environment(AccountModel.self) private var account
    @Environment(WatchLink.self) private var watchLink
    @Environment(\.scenePhase) private var scenePhase
    @State private var editor: EditorSession?
    @State private var cloudNotice = false
    #if DEBUG
    @State private var debugOpened = false
    #endif

    var body: some View {
        Group {
            if state.servers.isEmpty {
                emptyState
            } else {
                list
            }
        }
        .background(Color(.systemGroupedBackground))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("New agent", systemImage: "plus") { newAgent() }
                    .disabled(state.servers.isEmpty)
            }
        }
        .task(id: state.servers.map(\.id)) {
            await model.load()
            #if DEBUG
            openDebugRoute()
            #endif
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.load() } }
        }
        .sheet(item: $editor) { session in
            AgentEditorView(server: session.server, editing: session.editing) { server in
                await model.reload(server: server)
            }
            .closesForLinks()
        }
        .alert("Wristcall Cloud", isPresented: $cloudNotice) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(AgentsHomeView.cloudSoonMessage)
        }
    }

    // MARK: - With servers

    private var stripKind: WatchStripState.Kind {
        WatchStripState.make(
            paired: watchLink.isPaired, appInstalled: watchLink.isWatchAppInstalled,
            watchAccount: watchLink.watchAccount, myAccount: account.accountID, watchServers: watchLink.watchServers,
            sections: model.sections, lastContextAt: watchLink.lastContextAt
        )
    }

    private var list: some View {
        List {
            if stripKind != .hidden {
                Section { WatchStrip(kind: stripKind) }
            }
            if account.plan != nil {
                Section {
                    cloudRow
                } header: {
                    Text("WRISTCALL CLOUD · BETA")
                }
            }
            ForEach(model.sections) { section in
                Section {
                    rows(of: section)
                } header: {
                    Text(section.server.name.uppercased())
                }
            }
            if let footer = AgentSummary.footer(firstAgent: model.firstAgentName) {
                Section {} footer: { Text(footer) }
            }
        }
        .overlay {
            if !model.didLoad { ProgressView() }
        }
        .refreshable { await model.load() }
    }

    @ViewBuilder
    private func rows(of section: AllAgentsModel.Section) -> some View {
        if let error = section.error {
            HStack {
                Text(error).font(.subheadline).foregroundStyle(.orange)
                Spacer()
                Button("Retry") { Task { await model.reload(server: section.server) } }
                    .buttonStyle(.borderless)
            }
        }
        ForEach(section.agents) { agent in
            Button { editor = EditorSession(server: section.server, editing: agent) } label: {
                AgentRow(
                    icon: agent.icon, color: agent.color, name: agent.displayName,
                    subtitle: AgentSummary.subtitle(agent)
                ) {
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
            }
            .buttonStyle(.plain)
        }
        if section.agents.isEmpty && section.error == nil && model.didLoad {
            Text("No agents yet. Tap + to create one.").font(.subheadline).foregroundStyle(.secondary)
        }
    }

    private var cloudRow: some View {
        Button { cloudNotice = true } label: {
            AgentRow(icon: "note.text", color: .yellow, name: "Voice notes", subtitle: "One-shot · saved to History") {
                HStack(spacing: 8) {
                    Text("Soon")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.accentColor)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.accentColor.opacity(0.14), in: .rect(cornerRadius: 8))
                    Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
                }
            }
        }
        .buttonStyle(.plain)
    }

    // MARK: - Without servers

    private var emptyState: some View {
        VStack(spacing: 0) {
            if stripKind != .hidden {
                WatchStrip(kind: stripKind)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 22))
                    .padding(.horizontal, 16)
            }
            Spacer(minLength: 24)
            VStack(spacing: 0) {
                Image(systemName: "server.rack")
                    .font(.system(size: 36))
                    .foregroundStyle(.secondary)
                    .frame(width: 84, height: 84)
                    .background(Color(.tertiarySystemFill), in: .circle)
                    .accessibilityHidden(true)
                Text("Connect your server")
                    .font(.system(size: 22, weight: .bold))
                    .padding(.top, 18)
                Text("Start the wristcall server. It shows a QR code the first time it runs: scan it here.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.top, 8)
                Button(action: onScan) {
                    Label("Scan QR code", systemImage: "qrcode.viewfinder")
                        .font(.headline)
                        .frame(width: 210)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .controlSize(.large)
                .padding(.top, 24)
                Link("How to run a server", destination: AgentsHomeView.runServerURL)
                    .font(.body.weight(.medium))
                    .padding(.top, 14)
            }
            .padding(.horizontal, 40)
            Spacer(minLength: 24)
            if account.plan != nil {
                VStack(alignment: .leading, spacing: 6) {
                    Text("WRISTCALL CLOUD · BETA").font(.footnote).foregroundStyle(.secondary).padding(.leading, 16)
                    cloudRow
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 22))
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
            } else {
                HStack(spacing: 4) {
                    Text("Rather not run a server?").foregroundStyle(.secondary)
                    Button("Try Wristcall Cloud", action: onTryCloud).fontWeight(.semibold)
                }
                .font(.subheadline)
                .padding(.bottom, 12)
            }
        }
        .padding(.top, 4)
    }

    // MARK: - Editor

    /// "+": the editor opens on the first server that answered (it offers the others in its own picker).
    private func newAgent() {
        guard let server = model.sections.first(where: { $0.error == nil })?.server ?? state.servers.first else { return }
        editor = EditorSession(server: server, editing: nil)
    }

    #if DEBUG
    /// `-debugOpen newAgent` (or `editAgent`): opens the editor without a tap, for screenshots.
    private func openDebugRoute() {
        guard !debugOpened, let route = DebugRoute.value, ["newAgent", "editAgent"].contains(route) else { return }
        debugOpened = true
        if route == "newAgent" {
            newAgent()
        } else if let section = model.sections.first(where: { !$0.agents.isEmpty }), let agent = section.agents.last {
            editor = EditorSession(server: section.server, editing: agent)
        }
    }
    #endif
}

/// A row: badge, name, subtitle and whatever trails (chevron, "Soon").
private struct AgentRow<Trailing: View>: View {
    let icon: String
    let color: AgentColor?
    let name: String
    let subtitle: String
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 12) {
            AgentBadge(icon: icon, color: color, size: 40)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).foregroundStyle(.primary)
                Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            trailing
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}
