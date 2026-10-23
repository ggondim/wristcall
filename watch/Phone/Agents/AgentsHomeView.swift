import SwiftUI
import WristcallKit

/// The main tab (mockups `iphone-03` and `iphone-04`): the Apple Watch strip and one section per server with its
/// agents, the Wristcall Cloud entry first. Without any (the Cloud entry included) it asks to connect a server.
struct AgentsHomeView: View {
    /// "Scan QR code": the way to connect a server.
    let onScan: () -> Void
    /// "Try Wristcall Cloud".
    let onTryCloud: () -> Void

    /// What "How to run a server" opens: the README section.
    static let runServerURL = URL(string: "https://github.com/ggondim/wristcall#run-the-server-in-5-minutes")!

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
    @Environment(UsageModel.self) private var usage
    @Environment(WatchLink.self) private var watchLink
    @Environment(\.scenePhase) private var scenePhase
    @State private var editor: EditorSession?
    #if DEBUG
    @State private var debugOpened = false
    #endif

    private var layout: AgentsHomeLayout {
        AgentsHomeLayout(servers: state.allServers, hasPlan: account.plan != nil)
    }

    var body: some View {
        Group {
            if layout.isEmpty {
                emptyState
            } else {
                list
            }
        }
        .background(Color(.systemGroupedBackground))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("New agent", systemImage: "plus") { newAgent() }
                    .disabled(!layout.canCreateAgent)
            }
        }
        // D16: opening Agents reads the Cloud entry again (it comes and goes with the plan).
        .task { await account.refreshCloudEntry() }
        // The month's call minutes: on opening, and when the Cloud entry comes or goes.
        .task(id: state.cloudServer?.id) { await usage.load() }
        .task(id: state.allServers.map(\.id)) {
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
    }

    // MARK: - With servers

    private var stripKind: WatchStripState.Kind {
        WatchStripState.make(
            paired: watchLink.isPaired, appInstalled: watchLink.isWatchAppInstalled,
            cloudAvailable: account.cloudURL != nil, watchAccount: watchLink.watchAccount, myAccount: account.accountID,
            watchServers: watchLink.watchServers, sections: model.sections, lastContextAt: watchLink.lastContextAt
        )
    }

    private var list: some View {
        List {
            if stripKind != .hidden {
                Section { WatchStrip(kind: stripKind) }
            }
            if let warning = usage.usage?.warning {
                Section { usageBanner(warning) }
            }
            if layout.cloudNotice == .settingUp {
                Section {
                    settingUpRow
                } header: {
                    Text(AgentsHomeLayout.cloudHeader)
                }
            }
            ForEach(model.sections) { section in
                Section {
                    rows(of: section)
                } header: {
                    Text(AgentsHomeLayout.header(for: section.server))
                }
            }
            if let footer = AgentSummary.footer(firstAgent: model.firstAgentName) {
                Section {} footer: { Text(footer) }
            }
        }
        .overlay {
            if !model.didLoad { ProgressView() }
        }
        .refreshable {
            await account.refreshCloudEntry()
            await model.load()
            await usage.load()
        }
    }

    /// The 80 % warning (orange), or the month used up (red).
    private func usageBanner(_ text: String) -> some View {
        let color: Color = usage.usage?.level == .exhausted ? .red : .orange
        return Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundStyle(color)
            .accessibilityElement(children: .combine)
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

    /// A plan, and the Cloud has not added its entry to the agenda yet.
    private var settingUpRow: some View {
        HStack(spacing: 12) {
            ProgressView()
            Text(AgentsHomeLayout.settingUpText).font(.subheadline).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
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
            if layout.cloudNotice == .settingUp {
                VStack(alignment: .leading, spacing: 6) {
                    Text(AgentsHomeLayout.cloudHeader).font(.footnote).foregroundStyle(.secondary).padding(.leading, 16)
                    settingUpRow
                        .frame(maxWidth: .infinity, alignment: .leading)
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

    /// "+": the editor opens on the first own server that answered, the Cloud entry only without one (it offers the
    /// others in its own picker).
    private func newAgent() {
        guard let server = AgentsHomeLayout.newAgentServer(
            sections: model.sections, own: state.servers, cloud: state.cloudServer
        ) else { return }
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

/// A row: badge, name, subtitle and whatever trails (a chevron).
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
