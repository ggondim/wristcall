import SwiftUI
import WristcallKit

/// The History tab: the calls of every server in one list, with search, filters, export and "delete all".
struct HistoryView: View {
    @Environment(HistoryModel.self) private var model
    @Environment(AppState.self) private var state
    @State private var path: [String] = []

    var body: some View {
        NavigationStack(path: $path) {
            HistoryContent(model: model, serverIDs: state.servers.map(\.id), showsServer: state.servers.count > 1)
                .navigationTitle("History")
                .navigationDestination(for: String.self) { id in
                    CallDetailView(itemID: id)
                }
                #if DEBUG
                .task(id: model.items.first?.id) {
                    if DebugRoute.value == "history-detail", path.isEmpty,
                       let id = (model.items.first { $0.call.canRedeliver } ?? model.items.first)?.id { path = [id] }
                }
                #endif
        }
    }
}

/// What makes the list read again.
private struct ReloadTrigger: Equatable {
    var filter: HistoryModel.Filter
    var servers: [String]
}

private struct ExportedFile: Identifiable {
    let url: URL
    var id: URL { url }
}

private struct HistoryContent: View {
    @Bindable var model: HistoryModel
    let serverIDs: [String]
    let showsServer: Bool
    @Environment(AppState.self) private var state
    @State private var lastSearched = ""
    @State private var loaded = false
    @State private var exported: ExportedFile?
    @State private var confirmingDeleteAll = false

    private var trigger: ReloadTrigger { ReloadTrigger(filter: model.filter, servers: serverIDs) }

    var body: some View {
        List {
            ForEach(model.failures.sorted(by: { $0.key < $1.key }), id: \.key) { id, message in
                Label("\(name(of: id)): \(message)", systemImage: "exclamationmark.triangle")
                    .font(.footnote).foregroundStyle(.orange)
            }
            ForEach(model.items) { item in
                NavigationLink(value: item.id) { HistoryRow(item: item, showsServer: showsServer) }
            }
            if model.canLoadMore {
                Button {
                    Task { await model.loadMore() }
                } label: {
                    HStack {
                        Text("Load more")
                        if model.isLoadingMore { Spacer(); ProgressView() }
                    }
                }
                .disabled(model.isLoadingMore)
            }
        }
        .overlay { emptyState }
        .searchable(text: $model.filter.text, prompt: "Search what was said")
        .autocorrectionDisabled()
        .refreshable { await model.reload() }
        .task(id: trigger) {
            // Typing waits for a pause; any other change (server, agent, period) reads at once.
            if model.filter.text != lastSearched {
                try? await Task.sleep(for: .milliseconds(400))
                if Task.isCancelled { return }
            }
            lastSearched = model.filter.text
            await model.reload()
            loaded = true
        }
        .task(id: model.filter.serverID) { await model.loadAgents() }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { filterMenu }
            ToolbarItem(placement: .topBarTrailing) { moreMenu }
        }
        .sheet(item: $exported, onDismiss: { model.discardExport() }) { file in
            ExportSheet(url: file.url)
        }
        .confirmationDialog(
            deleteAllTitle, isPresented: $confirmingDeleteAll, titleVisibility: .visible
        ) {
            Button("Delete all", role: .destructive) { Task { await model.deleteAll() } }
        } message: {
            Text("Every call of \(model.filter.agentID == nil ? "this server" : "this agent") is deleted, not only the ones the search or period shows. This can't be undone.")
        }
        .alert("History", isPresented: Binding(
            get: { model.error != nil || model.notice != nil },
            set: { if !$0 { model.error = nil; model.notice = nil } }
        )) {
            Button("OK") { model.error = nil; model.notice = nil }
        } message: {
            Text(model.error ?? model.notice ?? "")
        }
    }

    @ViewBuilder private var emptyState: some View {
        if model.items.isEmpty && !model.isLoading && model.failures.isEmpty {
            if serverIDs.isEmpty {
                ContentUnavailableView(
                    "No servers", systemImage: "server.rack",
                    description: Text("Add a server to see its calls here."))
            } else if loaded && model.filter.isActive {
                ContentUnavailableView.search
            } else if loaded {
                ContentUnavailableView(
                    "No history yet", systemImage: "clock",
                    description: Text("Calls from servers will show up here."))
            }
        }
    }

    // MARK: Menus

    private var filterMenu: some View {
        Menu {
            if state.servers.count > 1 {
                Picker("Server", selection: Binding(get: { model.filter.serverID }, set: { model.selectServer($0) })) {
                    Text("All servers").tag(String?.none)
                    ForEach(state.servers) { server in Text(server.name).tag(String?.some(server.id)) }
                }
            }
            if model.filter.serverID != nil, !model.agents.isEmpty {
                Picker("Agent", selection: $model.filter.agentID) {
                    Text("All agents").tag(String?.none)
                    ForEach(model.agents) { agent in Text(agent.displayName).tag(String?.some(agent.id)) }
                }
            }
            Picker("Period", selection: Binding(get: { model.period }, set: { model.setPeriod($0) })) {
                ForEach(HistoryModel.Period.allCases) { period in Text(period.title).tag(period) }
            }
        } label: {
            Label("Filter", systemImage: model.filter.serverID != nil || model.period != .anytime
                  ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
        }
    }

    private var moreMenu: some View {
        Menu {
            if model.filter.serverID == nil {
                // One file per server: the menu says why the items are off.
                Text("Choose a server in the filter to export or delete all.")
            }
            Button("Export as Markdown", systemImage: "doc.text") { export(.markdown) }
                .disabled(model.filter.serverID == nil)
            Button("Export as JSON", systemImage: "curlybraces") { export(.json) }
                .disabled(model.filter.serverID == nil)
            Button("Delete all…", systemImage: "trash", role: .destructive) { confirmingDeleteAll = true }
                .disabled(model.filter.serverID == nil)
        } label: {
            Label("More", systemImage: "ellipsis.circle")
        }
    }

    private func export(_ format: ExportFormat) {
        Task {
            do {
                exported = ExportedFile(url: try await model.export(format))
            } catch let error as HistoryModel.ExportError {
                model.error = error.message
            } catch {
                model.error = "The export failed."
            }
        }
    }

    private var deleteAllTitle: String {
        let server = name(of: model.filter.serverID ?? "")
        if let agentID = model.filter.agentID {
            let agent = model.agents.first { $0.id == agentID || $0.slug == agentID }?.displayName ?? "this agent"
            return "Delete all calls of \(agent) on \(server)?"
        }
        return "Delete all calls on \(server)?"
    }

    private func name(of serverID: String) -> String {
        state.servers.first { $0.id == serverID }?.name ?? "the server"
    }
}

private struct ExportSheet: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                Image(systemName: "doc.badge.arrow.up").font(.system(size: 48)).foregroundStyle(.secondary)
                Text(url.lastPathComponent).font(.headline)
                Text("The file has the calls of the server, agent and period you chose. The search text does not apply.")
                    .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                ShareLink(item: url) { Label("Share", systemImage: "square.and.arrow.up") }
                    .buttonStyle(.borderedProminent)
            }
            .padding()
            .navigationTitle("Export")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.medium])
    }
}

struct HistoryRow: View {
    let item: HistoryModel.Item
    let showsServer: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle().fill(CallStatusStyle.color(item.call.status)).frame(width: 9, height: 9)
                    .accessibilityHidden(true)
                Text(item.call.agent?.displayName ?? "Unknown agent").font(.headline)
                Spacer()
                Text(Date(timeIntervalSince1970: item.call.createdAt)
                    .formatted(.relative(presentation: .numeric, unitsStyle: .abbreviated)))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let text = preview {
                Text(text).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 6) {
                Text(CallStatusStyle.label(item.call)).foregroundStyle(CallStatusStyle.color(item.call.status))
                if showsServer {
                    Text("·").foregroundStyle(.tertiary)
                    Text(item.serverName).foregroundStyle(.secondary)
                }
            }
            .font(.caption)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var preview: String? {
        let text = item.call.text ?? item.call.entries.compactMap(\.text).first
        guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return text
    }
}

/// Colors and words for a call status: delivered green, failed red, everything else (processing, empty,
/// ended, unknown) gray. The word is always shown, so the color is never the only cue.
enum CallStatusStyle {
    static func color(_ status: String) -> Color {
        switch status {
        case "delivered": .green
        case "failed": .red
        default: .gray
        }
    }

    static func label(_ call: CallRecord) -> String {
        switch call.status {
        case "delivered": "Delivered"
        case "failed": call.error.map { "Failed, \(reason($0))" } ?? "Failed"
        case "processing": "Processing"
        case "recording": "Recording"
        case "empty": "Nothing said"
        case "ended": "Ended"
        default: call.status.capitalized
        }
    }

    static func reason(_ code: String) -> String {
        switch code {
        case "delivery_failed": "not delivered"
        case "interrupted": "interrupted"
        case "stt_failed": "not transcribed"
        case "responder_failed": "no answer"
        case "tts_failed": "no voice"
        case "unreadable": "unreadable"
        default: code.replacingOccurrences(of: "_", with: " ")
        }
    }
}
