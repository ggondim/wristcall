import SwiftUI

/// Picks the screen for `AppModel.phase`. Each screen owns its `NavigationStack`, so a phase
/// change (unpair from Settings, for example) also drops whatever was pushed.
struct RootView: View {
    let model: AppModel
    @Environment(AccountLoginModel.self) private var login
    @Environment(SetupModel.self) private var setup: SetupModel?

    var body: some View {
        // The account login stays up while it adds servers underneath (the phase moves to Home), but a call
        // and its result come first.
        if login.isPresented, !model.isInCallOrResult {
            NavigationStack {
                AccountLoginView(login: login)
            }
        } else {
            screen
        }
    }

    @ViewBuilder
    private var screen: some View {
        switch model.phase {
        case .launching:
            ProgressView()
        case .unpaired:
            // No server: the setup screen, unless pairing was asked for or the build has no Cloud.
            if let setup, setup.showsSetup {
                SetupView(model: model, setup: setup)
            } else {
                PairingView(model: model)
            }
        case .pairing(requestId: nil):
            PairingView(model: model)
        case .pairing(let requestId?):
            ApprovalView(model: model, requestId: requestId)
        case .home, .unavailable:
            HomeView(model: model)
        case .inCall(let target):
            InCallView(model: model, target: target)
        case .callResult:
            if let result = model.callResult {
                CallResultView(model: model, result: result)
            } else {
                ProgressView()
            }
        }
    }
}
