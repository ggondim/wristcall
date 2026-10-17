import Testing
@testable import Wristcall

@MainActor
struct SetupViewTests {
    @Test func stepsFollowThePhase() {
        let signingIn = SetupView.steps(for: .signingIn)
        #expect(signingIn.signIn == .active)
        #expect(signingIn.agents == .pending)

        let gettingAgents = SetupView.steps(for: .gettingAgents)
        #expect(gettingAgents.signIn == .done)
        #expect(gettingAgents.agents == .active)

        let ready = SetupView.steps(for: .ready)
        #expect(ready.signIn == .done)
        #expect(ready.agents == .done)
    }
}
