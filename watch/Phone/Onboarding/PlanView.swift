import Observation
import SwiftUI
import WristcallKit

/// The three ways to start (spec 4.1): the two Cloud plans (invite-only beta for now) and the user's own server.
enum PlanOption: CaseIterable, Identifiable {
    case cloud, cloudCustom, ownServer

    var id: Self { self }
    var isCloud: Bool { self != .ownServer }

    var title: String {
        switch self {
        case .cloud: "Wristcall Cloud"
        case .cloudCustom: "Cloud with your models"
        case .ownServer: "Your own server"
        }
    }

    var detail: String {
        switch self {
        case .cloud: "We run the servers and the voice models. You bring your agent."
        case .cloudCustom: "Our servers, your speech-to-text and voice URLs."
        case .ownServer: "Run the open-source server and connect it with a QR code."
        }
    }

    var points: [String] {
        switch self {
        case .cloud: ["Voice notes agent ready to use", "600 call minutes a month"]
        case .cloudCustom: ["600 call minutes a month"]
        case .ownServer: ["Your data stays on your machine"]
        }
    }

    /// Shown on the card of the plan the account has (Settings).
    static let currentLabel = "Current plan"

    /// The card of `plan`; a plan this build does not know is the Cloud, like the Settings row.
    func isCurrent(for plan: Plan?) -> Bool {
        guard let plan else { return false }
        switch plan.plan {
        case .full, .other: return self == .cloud
        case .custom: return self == .cloudCustom
        }
    }

    var symbol: String { isCloud ? "cloud" : "server.rack" }
    var primaryTitle: String { isCloud ? "Enter invite code" : "Connect your server" }
}

/// What the plan screen does with a tap: a Cloud choice needs a redeemed invite to finish, the others finish
/// at once. The views only draw this.
@MainActor
@Observable
final class PlanModel {
    var selection: PlanOption = .cloud
    /// The invite sheet is up.
    var enteringCode = false
    var code = ""
    private(set) var error: String?
    private(set) var redeeming = false

    @ObservationIgnored private let redeemInvite: (String) async throws -> Plan
    @ObservationIgnored private let onFinish: (PlanChoice) -> Void
    /// The plan the account has when the screen opens from Settings; `nil` in onboarding.
    let currentPlan: Plan?

    init(
        redeemInvite: @escaping (String) async throws -> Plan, onFinish: @escaping (PlanChoice) -> Void,
        currentPlan: Plan? = nil
    ) {
        self.redeemInvite = redeemInvite
        self.onFinish = onFinish
        self.currentPlan = currentPlan
    }

    convenience init(account: AccountModel, currentPlan: Plan? = nil, onFinish: @escaping (PlanChoice) -> Void) {
        self.init(redeemInvite: { try await account.redeemInvite($0) }, onFinish: onFinish, currentPlan: currentPlan)
    }

    /// The main button; the card of the current plan has nothing to enter an invite for.
    var showsPrimaryButton: Bool { !selection.isCurrent(for: currentPlan) }

    /// The main button: Cloud asks for the invite first, the own server finishes.
    func primaryTapped() {
        if selection.isCloud {
            error = nil
            enteringCode = true
        } else {
            onFinish(.ownServer)
        }
    }

    func notNow() { onFinish(.notNow) }

    /// The code as the Cloud wants it: no spaces around, upper case.
    var normalizedCode: String { code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() }

    var canRedeem: Bool { !normalizedCode.isEmpty && !redeeming }

    func redeem() async {
        guard canRedeem else { return }
        redeeming = true
        error = nil
        defer { redeeming = false }
        do {
            let plan = try await redeemInvite(normalizedCode)
            enteringCode = false
            code = ""
            onFinish(.cloud(plan))
        } catch is CancellationError {
            // Nothing to say.
        } catch {
            self.error = Self.message(for: error)
        }
    }

    static let invalidCodeMessage = "That invite code isn't valid or was already used."
    static let tooManyMessage = "Too many attempts. Try again in a few minutes."
    static let settingUpMessage = "Setting up your account…"

    static func message(for error: any Error) -> String {
        switch error {
        case APIError.invalidCode: invalidCodeMessage
        case APIError.retryLater, APIError.rateLimited: tooManyMessage
        default: AccountModel.message(for: error)
        }
    }

    /// Why an invite cannot be redeemed yet (`nil`: it can). A Cloud account that is still being made reads
    /// "Setting up your account…".
    static func blocker(for state: AccountModel.State) -> String? {
        switch state {
        case .ready: nil
        case .starting: settingUpMessage
        case .waiting: "Can't reach wristcall Cloud. Trying again soon."
        case .needsSignIn: "Sign in to your account first."
        case .unavailable: "wristcall Cloud is not set up for this app."
        case .failed(let message): message
        }
    }
}

/// "How do you want to start?" (mockup `iphone-02`). `onFinish` gets the choice; Cloud only after an invite.
/// Reads the account from the environment.
struct PlanView: View {
    enum Mode { case onboarding, settings }

    let mode: Mode
    let onFinish: (PlanChoice) -> Void
    @Environment(AccountModel.self) private var account

    init(mode: Mode, onFinish: @escaping (PlanChoice) -> Void) {
        self.mode = mode
        self.onFinish = onFinish
    }

    var body: some View {
        PlanContent(mode: mode, account: account, onFinish: onFinish)
    }
}

private struct PlanContent: View {
    let mode: PlanView.Mode
    @State private var model: PlanModel

    init(mode: PlanView.Mode, account: AccountModel, onFinish: @escaping (PlanChoice) -> Void) {
        self.mode = mode
        // Only Settings knows a current plan; onboarding is the way to get one.
        _model = State(initialValue: PlanModel(
            account: account, currentPlan: mode == .settings ? account.plan : nil, onFinish: onFinish))
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("How do you want to start?")
                    .font(.system(size: 30, weight: .bold))
                    .padding(.horizontal, 4)
                Text("You can change this any time in Settings.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .padding(.bottom, 4)
                ForEach(PlanOption.allCases) { option in
                    PlanCard(
                        option: option, selected: model.selection == option,
                        isCurrent: option.isCurrent(for: model.currentPlan)
                    ) { model.selection = option }
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
        }
        .background(Color(.systemGroupedBackground))
        .safeAreaInset(edge: .bottom) { footer }
        .sheet(isPresented: $model.enteringCode) {
            InviteCodeSheet(model: model).closesForLinks()
        }
    }

    private var footer: some View {
        VStack(spacing: 8) {
            if model.showsPrimaryButton {
                Button { model.primaryTapped() } label: {
                    Text(model.selection.primaryTitle).font(.headline).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .controlSize(.large)
            }
            Text("Wristcall Cloud is in invite-only beta. Your own server is always free.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if mode == .onboarding {
                Button("Not now") { model.notNow() }
                    .font(.body.weight(.medium))
                    .padding(.vertical, 4)
            }
        }
        .padding(.horizontal, 20)
        .padding(.top, 8)
        .padding(.bottom, 4)
        .background(Color(.systemGroupedBackground))
    }
}

private struct PlanCard: View {
    let option: PlanOption
    let selected: Bool
    /// The plan the account has (Settings).
    var isCurrent = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: option.symbol)
                    .font(.system(size: 19, weight: .regular))
                    .foregroundStyle(option == .cloud ? Color.white : Color(.systemBackground))
                    .frame(width: 38, height: 38)
                    .background(option == .cloud ? Color.accentColor : Color(.label), in: .rect(cornerRadius: 11))
                VStack(alignment: .leading, spacing: 4) {
                    if isCurrent {
                        Text(PlanOption.currentLabel)
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Color.green, in: .rect(cornerRadius: 8))
                    } else if option.isCloud {
                        Text("INVITE-ONLY BETA")
                            .font(.system(size: 11, weight: .bold))
                            .tracking(0.4)
                            .foregroundStyle(Color.accentColor)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(Color.accentColor.opacity(0.14), in: .rect(cornerRadius: 8))
                    }
                    Text(option.title).font(.headline).foregroundStyle(.primary)
                    Text(option.detail)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.leading)
                    ForEach(option.points, id: \.self) { point in
                        Label {
                            Text(point).font(.footnote).foregroundStyle(.secondary)
                        } icon: {
                            Image(systemName: "checkmark").font(.caption.weight(.bold)).foregroundStyle(Color.accentColor)
                        }
                    }
                    if option == .ownServer {
                        Text("Free").font(.subheadline.weight(.semibold)).foregroundStyle(.primary).padding(.top, 4)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.title2)
                    .foregroundStyle(selected ? Color.accentColor : Color(.tertiaryLabel))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 13)
            .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 22))
            .overlay(
                RoundedRectangle(cornerRadius: 22)
                    .strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 2)
            )
            .contentShape(.rect(cornerRadius: 22))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}

/// "Enter invite code": one field and "Redeem".
struct InviteCodeSheet: View {
    @Bindable var model: PlanModel
    @Environment(AccountModel.self) private var account
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    private var blocker: String? { PlanModel.blocker(for: account.state) }
    /// The account is still being made: the button itself says so.
    private var isSettingUp: Bool { account.state == .starting }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Invite code", text: $model.code, prompt: Text("WC-XXXX-XXXX-XXXX"))
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .keyboardType(.asciiCapable)
                        .font(.body.monospaced())
                        .focused($focused)
                        .submitLabel(.go)
                        .onSubmit { redeem() }
                } footer: {
                    if let blocker, !isSettingUp {
                        Text(blocker)
                    }
                }
                if let error = model.error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button { redeem() } label: {
                        HStack {
                            Text(isSettingUp ? PlanModel.settingUpMessage : "Redeem")
                            if model.redeeming || isSettingUp {
                                Spacer()
                                ProgressView()
                            }
                        }
                    }
                    .disabled(!model.canRedeem || blocker != nil)
                }
            }
            .navigationTitle("Invite code")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(model.redeeming)
                }
            }
            .interactiveDismissDisabled(model.redeeming)
            .onAppear { focused = true }
        }
        .presentationDetents([.medium, .large])
    }

    private func redeem() {
        guard blocker == nil else { return }
        Task { await model.redeem() }
    }
}
