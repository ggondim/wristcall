import SwiftUI
import UIKit
import WristcallKit

/// One call: what was said, how the delivery went, and what can be done with it. Reads the call from the
/// model by id, so a redelivery or a refresh shows here at once.
struct CallDetailView: View {
    let itemID: String
    @Environment(HistoryModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var confirmingDelete = false
    @State private var copied = false

    private var item: HistoryModel.Item? { model.items.first { $0.id == itemID } }

    var body: some View {
        Group {
            if let item {
                content(item)
            } else {
                ContentUnavailableView("Call not found", systemImage: "phone.down",
                                       description: Text("It was deleted or is no longer in the list."))
            }
        }
        .navigationTitle("Call")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .tabBar)
    }

    private func content(_ item: HistoryModel.Item) -> some View {
        let call = item.call
        return List {
            Section {
                LabeledContent("Agent", value: call.agent?.displayName ?? "Unknown agent")
                LabeledContent("Server", value: item.serverName)
                LabeledContent("When", value: Date(timeIntervalSince1970: call.createdAt)
                    .formatted(date: .abbreviated, time: .shortened))
                LabeledContent("Type", value: Self.typeLabel(call.callType))
                LabeledContent("Status") {
                    Text(CallStatusStyle.label(call)).foregroundStyle(CallStatusStyle.color(call.status))
                }
                if let expires = call.expiresAt {
                    LabeledContent("Kept until", value: Date(timeIntervalSince1970: expires)
                        .formatted(date: .abbreviated, time: .omitted))
                }
            }

            Section("Delivery") {
                LabeledContent("Attempts", value: "\(call.attempts)")
                if let status = call.lastHttpStatus {
                    LabeledContent("Last answer", value: "HTTP \(status)")
                }
            }

            Section("Transcript") {
                if call.entries.isEmpty, let text = call.text, !text.isEmpty {
                    Text(text).textSelection(.enabled)
                } else if call.entries.isEmpty {
                    Text("Nothing was said.").foregroundStyle(.secondary)
                }
                ForEach(Array(call.entries.enumerated()), id: \.offset) { _, entry in
                    EntryRow(entry: entry)
                }
            }

            Section {
                Button(copied ? "Copied" : "Copy transcript", systemImage: "doc.on.doc") {
                    UIPasteboard.general.string = call.transcript
                    copied = true
                }
                .disabled(call.transcript.isEmpty)
                if call.canRedeliver {
                    Button("Redeliver", systemImage: "arrow.clockwise") { Task { await model.redeliver(item) } }
                }
                Button("Delete", systemImage: "trash", role: .destructive) { confirmingDelete = true }
            }
        }
        .task(id: call.status) { await follow(item) }
        .confirmationDialog("Delete this call?", isPresented: $confirmingDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                Task {
                    await model.delete(item)
                    if model.error == nil { dismiss() }
                }
            }
        } message: {
            Text("It is deleted from \(item.serverName). This can't be undone.")
        }
        .alert("Call", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("OK") { model.error = nil }
        } message: {
            Text(model.error ?? "")
        }
    }

    /// While the server is still working on the call (a redelivery just started), asks again every few seconds.
    private func follow(_ item: HistoryModel.Item) async {
        var tries = 0
        while ["processing", "recording"].contains(item.call.status), tries < 40 {
            try? await Task.sleep(for: .seconds(3))
            if Task.isCancelled { return }
            await model.refresh(item)
            tries += 1
        }
    }

    static func typeLabel(_ type: String) -> String {
        switch type {
        case "conversation": "Conversation"
        case "one-shot": "One-shot"
        case "monologue": "Monologue"
        default: type
        }
    }
}

private struct EntryRow: View {
    let entry: CallEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(entry.role == "user" ? "You" : "Agent").font(.caption).foregroundStyle(.secondary)
            if let text = entry.text, !text.isEmpty {
                Text(text).textSelection(.enabled)
            } else if let error = entry.error {
                Text(CallStatusStyle.reason(error).capitalized).foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 2)
    }
}
