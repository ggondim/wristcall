import SwiftUI
import WristcallKit

/// One server's devices: requests waiting for approval on top, then the paired devices, and the way to
/// get a pairing code.
struct DevicesView: View {
    let server: ManagedServer

    @Environment(AppState.self) private var state
    @Environment(ApprovalsModel.self) private var approvals
    @State private var model: DevicesModel?

    var body: some View {
        Group {
            if let model {
                DevicesContent(server: server, model: model, approvals: approvals)
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Devices")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if model == nil { model = DevicesModel(api: state.api(for: server)) }
        }
        #if WRISTCALL_PUSH
        // Approvals are what this screen is about: ask for notifications if never asked (push build).
        .task { await PhoneAppDelegate.push?.devicesAppeared(server) }
        #endif
    }
}

private struct DevicesContent: View {
    let server: ManagedServer
    let model: DevicesModel
    let approvals: ApprovalsModel

    @State private var approving: ApprovalsModel.Pending?
    @State private var revoking: DeviceRecord?
    #if DEBUG
    @State private var debugCode = false
    #endif
    @State private var loaded = false

    private var waiting: [ApprovalsModel.Pending] { approvals.pending.filter { $0.serverID == server.id } }

    var body: some View {
        List {
            if let error = model.error {
                Text(error).foregroundStyle(.red)
            }

            if !waiting.isEmpty {
                Section {
                    ForEach(waiting) { item in
                        ApprovalRow(item: item, busy: approvals.busy.contains(item.id),
                                    approve: { approving = item },
                                    deny: { Task { await approvals.deny(item) } })
                    }
                } header: {
                    Text("Waiting approval")
                } footer: {
                    Text("A watch asked to sign in. Approve only a watch you started.")
                }
            }

            Section("Devices") {
                if model.devices.isEmpty && loaded {
                    Text("No watches paired yet.").foregroundStyle(.secondary)
                }
                ForEach(model.devices) { device in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(device.name)
                        Text(Date(timeIntervalSince1970: device.createdAt).formatted(date: .abbreviated, time: .shortened))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                        Button("Revoke", role: .destructive) { revoking = device }
                    }
                }
            }

            Section {
                NavigationLink {
                    PairingCodeView(model: model)
                } label: {
                    Label("Pairing code", systemImage: "number.square")
                }
            } footer: {
                Text("Pair a watch by typing an 8 digit code on it.")
            }
        }
        #if DEBUG
        .navigationDestination(isPresented: $debugCode) { PairingCodeView(model: model) }
        #endif
        .refreshable {
            await model.load()
            await approvals.refresh()
        }
        .task {
            await model.load()
            loaded = true
            #if DEBUG
            debugCode = DebugRoute.value == "code"
            #endif
            // While this screen is open, ask the servers for new requests every 10 seconds.
            while !Task.isCancelled {
                await approvals.refresh()
                try? await Task.sleep(for: .seconds(10))
            }
        }
        .confirmationDialog(
            "Approve \(approving?.request.deviceName ?? "this watch")?",
            isPresented: Binding(get: { approving != nil }, set: { if !$0 { approving = nil } }),
            titleVisibility: .visible, presenting: approving
        ) { item in
            Button("Approve") { Task { await approvals.approve(item) } }
        } message: { _ in
            Text("It will be able to call your agents on \(server.name) until you revoke it.")
        }
        .confirmationDialog(
            "Revoke \(revoking?.name ?? "this device")?",
            isPresented: Binding(get: { revoking != nil }, set: { if !$0 { revoking = nil } }),
            titleVisibility: .visible, presenting: revoking
        ) { device in
            Button("Revoke", role: .destructive) { Task { await model.revoke(device) } }
        } message: { _ in
            Text("The watch stops working with this server until it is paired again.")
        }
        .alert(
            "Request", isPresented: Binding(get: { approvals.notice != nil }, set: { if !$0 { approvals.notice = nil } })
        ) {
            Button("OK") { approvals.notice = nil }
        } message: {
            Text(approvals.notice ?? "")
        }
    }
}

private struct ApprovalRow: View {
    let item: ApprovalsModel.Pending
    let busy: Bool
    let approve: () -> Void
    let deny: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(item.request.deviceName).font(.headline)
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let left = Int(item.request.expiresAt - context.date.timeIntervalSince1970)
                VStack(alignment: .leading, spacing: 8) {
                    Text(left > 0 ? "Expires in \(PairingCodeView.countdown(left))" : "Expired")
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    HStack {
                        Button("Approve", action: approve).buttonStyle(.borderedProminent)
                        Button("Deny", role: .destructive, action: deny).buttonStyle(.bordered)
                    }
                    .disabled(busy || left <= 0)
                }
            }
        }
        .padding(.vertical, 4)
    }
}
