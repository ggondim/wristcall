import SwiftUI

/// The first screen (mockup `iphone-01`): the brand, three promises, the voice notice and "Get started".
/// The silent account is made meanwhile (`account.start()`, single-flight), so the plan screen finds it ready.
struct WelcomeView: View {
    @Environment(AccountModel.self) private var account
    let onContinue: () -> Void
    let onRestored: () -> Void
    @State private var restoring = false

    private struct Feature: Identifiable {
        let symbol: String
        let title: String
        let text: String
        var id: String { title }
    }

    private static let features = [
        Feature(symbol: "lock", title: "No sign-up",
                text: "Your account is created on this iPhone and follows you to new devices with iCloud Keychain."),
        Feature(symbol: "applewatch", title: "Apple Watch, set up for you",
                text: "Open wristcall on your watch and your agents are already there."),
        Feature(symbol: "sparkles", title: "Bring any agent",
                text: "An OpenAI-compatible endpoint or a webhook. Your agent, your rules."),
    ]

    static var voiceNotice: AttributedString {
        let text = "When you call an agent, your voice is sent to the speech and AI services of that agent. "
        let markdown = text + "[Privacy](\(SettingsContent.privacyURL.absoluteString))"
        return (try? AttributedString(markdown: markdown)) ?? AttributedString(text + "Privacy")
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                VStack(spacing: 6) {
                    Image("BrandMark")
                        .resizable()
                        .scaledToFit()
                        .frame(width: 92, height: 92)
                        .accessibilityHidden(true)
                    Text("wristcall")
                        .font(.system(size: 34, weight: .bold))
                        .padding(.top, 12)
                    Text("Call your AI agents from your wrist.")
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.top, 36)
                VStack(alignment: .leading, spacing: 22) {
                    ForEach(Self.features) { feature in
                        HStack(alignment: .top, spacing: 14) {
                            Image(systemName: feature.symbol)
                                .font(.system(size: 24))
                                .foregroundStyle(.tint)
                                .frame(width: 30)
                                .padding(.top, 2)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(feature.title).font(.callout.weight(.semibold))
                                Text(feature.text).font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
                .padding(.top, 40)
            }
            .padding(.horizontal, 32)
        }
        .safeAreaInset(edge: .bottom) { footer }
        .task {
            // The unit tests run hosted in this app: no account is made from them.
            guard !PhoneApp.isUnitTest else { return }
            await account.start()
        }
        .sheet(isPresented: $restoring) {
            RestoreView(account: account, allowsStartOver: false, onClose: { restoring = false }) {
                restoring = false
                onRestored()
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 10) {
            Text(Self.voiceNotice)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button(action: onContinue) {
                Text("Get started").font(.headline).frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .buttonBorderShape(.capsule)
            .controlSize(.large)
            Button("Already use wristcall? Restore") { restoring = true }
                .font(.subheadline.weight(.medium))
                .padding(.vertical, 4)
        }
        .padding(.horizontal, 32)
        .padding(.top, 8)
        .padding(.bottom, 4)
        .background(.background)
    }
}
