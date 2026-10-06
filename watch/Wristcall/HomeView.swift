import SwiftUI
import WristcallKit

/// Home: profile name, status and the "Call" button.
struct HomeView: View {
    let model: AppModel

    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                Text(model.profile?.displayName ?? "wristcall")
                    .font(.title3)
                    .lineLimit(1)
                Text(status)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button {
                    model.startCall()
                } label: {
                    Label("Call", systemImage: "phone.fill")
                        .font(.title3)
                        .frame(maxWidth: .infinity, minHeight: 56)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .disabled(!model.canCall)
                if model.phase == .unavailable {
                    Button("Retry") {
                        Task { await model.retry() }
                    }
                }
            }
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
        }
    }

    private var status: String {
        if let message = model.message { return message }
        return model.canCall ? "Ready" : AppModel.Message.unreachable
    }
}

/// Placeholder call screen; tasks 8 to 10 put CallKit and audio behind it.
struct InCallView: View {
    let model: AppModel
    let profile: Profile?

    var body: some View {
        VStack(spacing: 8) {
            Text(profile?.displayName ?? "wristcall")
                .font(.title3)
            Text("In call")
                .font(.footnote)
                .foregroundStyle(.secondary)
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
