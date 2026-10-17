import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

@MainActor
struct OnboardingTests {
    let world = AccountWorld()

    func freshDefaults() -> UserDefaults { UserDefaults(suiteName: "onboarding-tests-" + UUID().uuidString)! }

    /// A silent account `acc_1` with its key, signed in here (no plan).
    func readyRig() async -> AccountRig {
        let key = StoredAccountKey.of("acc_1", age: 60)
        world.addAccount("acc_1", key: key.key, AccountWorld.Account())
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: [key])
        _ = await rig.started()
        return rig
    }

    /// A plan screen that records what it ended with.
    final class Outcome { var choice: PlanChoice? }

    func planModel(_ account: AccountModel, _ outcome: Outcome) -> PlanModel {
        PlanModel(account: account) { outcome.choice = $0 }
    }

    // MARK: - Gate

    @Test func needsSignInShowsRestore() async {
        world.refuseRefresh = true
        let rig = AccountRig(world, zitadel: TokenSet(
            accessToken: "fresh-at", refreshToken: "rt", expiresAt: AccountWorld.clock.addingTimeInterval(3600), idToken: "idt"
        ))
        let model = await rig.started()

        #expect(model.state == .needsSignIn)
        #expect(OnboardingGate.showsRestore(for: model.state))
        #expect(!OnboardingGate.showsRestore(for: .ready))
        #expect(!OnboardingGate.showsRestore(for: .starting))
        #expect(!OnboardingGate.showsRestore(for: .waiting(retryAt: nil)))
    }

    @Test func freshInstallShowsWelcome() {
        #expect(OnboardingGate.entryStep(isDone: false, serversLoaded: true, hasServers: false, didMigrate: false) == .welcome)
        // The servers are still being read: neither the welcome nor the tabs flash.
        #expect(OnboardingGate.entryStep(isDone: false, serversLoaded: false, hasServers: false, didMigrate: false) == nil)
    }

    @Test func unreadableServerStoreNeverShowsTheWelcome() async {
        let flaky = AppStateTests.FlakyStore([])
        let state = AppState(store: flaky, makeAPI: { _, _ in FakeServerAPI() })
        await state.load()

        // The read failed: an existing user (upgrade) goes to the tabs, where `loadError` is shown.
        #expect(state.loadError != nil)
        #expect(!state.didLoad)
        #expect(OnboardingGate.entryStep(
            isDone: false, serversLoaded: state.didLoad, hasServers: !state.servers.isEmpty, didMigrate: false
        ) == nil)
        #expect(!OnboardingGate.shouldFinishSilently(
            isDone: false, serversLoaded: state.didLoad, hasServers: !state.servers.isEmpty, didMigrate: false
        ))

        // A later successful read is a real "no servers": the welcome may show.
        flaky.readable = true
        await state.load()
        #expect(state.didLoad)
        #expect(OnboardingGate.entryStep(
            isDone: false, serversLoaded: state.didLoad, hasServers: !state.servers.isEmpty, didMigrate: false
        ) == .welcome)
    }

    @Test func onboardingFlagReadsTheSameDefaultsAsTheView() {
        // `RootView` reads `@AppStorage(OnboardingStore.key)` (UserDefaults.standard).
        #expect(OnboardingStore.defaults === UserDefaults.standard)
    }

    @Test func resetWinsOverADoneFlagFromAnotherDomain() {
        // A "done" the app cannot remove, from a domain below its own (on the simulator, a `simctl spawn defaults
        // write` lands in the device's preferences, which the app reads but never writes): reset still means
        // "not done", so the welcome comes back.
        let below = "onboarding-tests-below-" + UUID().uuidString
        let other = UserDefaults(suiteName: below)!
        other.set(true, forKey: OnboardingStore.key)
        defer { other.removePersistentDomain(forName: below) }
        let defaults = freshDefaults()
        defaults.addSuite(named: below)
        #expect(OnboardingStore.isDone(in: defaults))

        OnboardingStore.reset(in: defaults)

        #expect(!OnboardingStore.isDone(in: defaults))
        OnboardingStore.finish(in: defaults)
        #expect(OnboardingStore.isDone(in: defaults))
    }

    @Test func doneOnboardingGoesToTheTabs() {
        #expect(OnboardingGate.entryStep(isDone: true, serversLoaded: true, hasServers: false, didMigrate: false) == nil)
    }

    @Test func existingServersSkipWelcome() {
        #expect(OnboardingGate.entryStep(isDone: false, serversLoaded: true, hasServers: true, didMigrate: false) == nil)
        #expect(OnboardingGate.shouldFinishSilently(isDone: false, serversLoaded: true, hasServers: true, didMigrate: false))
        #expect(!OnboardingGate.shouldFinishSilently(isDone: true, serversLoaded: true, hasServers: true, didMigrate: false))
        // Not known yet: nothing is written.
        #expect(!OnboardingGate.shouldFinishSilently(isDone: false, serversLoaded: false, hasServers: false, didMigrate: false))
        #expect(!OnboardingGate.shouldFinishSilently(isDone: false, serversLoaded: true, hasServers: false, didMigrate: false))
    }

    @Test func migrationSkipsWelcome() {
        #expect(OnboardingGate.entryStep(isDone: false, serversLoaded: true, hasServers: false, didMigrate: true) == nil)
        #expect(OnboardingGate.shouldFinishSilently(isDone: false, serversLoaded: true, hasServers: false, didMigrate: true))
    }

    // MARK: - Plan

    @Test func notNowFinishesOnboarding() async {
        let rig = await readyRig()
        let outcome = Outcome()
        let plan = planModel(rig.model, outcome)

        plan.notNow()

        #expect(outcome.choice == .notNow)
        let defaults = freshDefaults()
        #expect(!OnboardingStore.isDone(in: defaults))
        #expect(OnboardingGate.finish(with: .notNow, in: defaults) == false)
        #expect(OnboardingStore.isDone(in: defaults))
    }

    @Test func cloudChoiceNeedsARedeemedInvite() async throws {
        let rig = await readyRig()
        let outcome = Outcome()
        let plan = planModel(rig.model, outcome)
        #expect(plan.selection == .cloud)

        plan.primaryTapped()

        // The invite sheet opens; nothing finished.
        #expect(plan.enteringCode)
        #expect(outcome.choice == nil)
        #expect(!plan.canRedeem)

        plan.code = " wc-aaaa-bbbb-cccc "
        await plan.redeem()

        #expect(outcome.choice == .cloud(Plan(plan: .full, source: "invite")))
        #expect(!plan.enteringCode)
        #expect(plan.error == nil)
        let sent = try #require(world.cloudRequests("POST", "/v1/entitlements/redeem").first)
        #expect(try sent.json()["code"] as? String == "WC-AAAA-BBBB-CCCC")
    }

    @Test func customCloudChoiceAlsoNeedsAnInvite() async {
        let rig = await readyRig()
        let outcome = Outcome()
        let plan = planModel(rig.model, outcome)
        plan.selection = .cloudCustom

        plan.primaryTapped()

        #expect(plan.enteringCode)
        #expect(outcome.choice == nil)
    }

    @Test func ownServerOpensTheScanner() {
        let outcome = Outcome()
        let plan = PlanModel(redeemInvite: { _ in throw APIError.invalidCode }, onFinish: { outcome.choice = $0 })
        plan.selection = .ownServer
        #expect(plan.selection.primaryTitle == "Connect your server")

        plan.primaryTapped()

        #expect(outcome.choice == .ownServer)
        #expect(!plan.enteringCode)
        let defaults = freshDefaults()
        #expect(OnboardingGate.finish(with: .ownServer, in: defaults))
        #expect(OnboardingStore.isDone(in: defaults))
    }

    @Test func failedRedeemKeepsTheSheetWithTheMessage() async {
        let outcome = Outcome()
        let failures: [(any Error, String)] = [
            (APIError.invalidCode, "That invite code isn't valid or was already used."),
            (APIError.rateLimited, "Too many attempts. Try again in a few minutes."),
            (APIError.retryLater(120), "Too many attempts. Try again in a few minutes."),
        ]
        for (failure, message) in failures {
            let plan = PlanModel(redeemInvite: { _ in throw failure }, onFinish: { outcome.choice = $0 })
            plan.primaryTapped()
            plan.code = "WC-AAAA-BBBB-CCCC"

            await plan.redeem()

            #expect(plan.error == message)
            #expect(plan.enteringCode)
            #expect(!plan.redeeming)
        }
        #expect(outcome.choice == nil)
    }

    @Test func redeemWaitsForTheAccount() {
        #expect(PlanModel.blocker(for: .starting) == "Setting up your account…")
        #expect(PlanModel.blocker(for: .ready) == nil)
        #expect(PlanModel.blocker(for: .waiting(retryAt: nil)) != nil)
    }

    // MARK: - Restore

    @Test func restoreAsksBeforeLeavingAnAccountWithServers() async {
        var calls: [Bool] = []
        var restored = 0
        let restore = RestoreModel(
            restore: { leaving in
                calls.append(leaving)
                if !leaving { throw RestoreError.currentAccountHasData(servers: 3) }
            },
            startOver: {},
            onRestored: { restored += 1 }
        )

        await restore.signIn()

        #expect(restore.leavingServers == 3)
        #expect(restore.isConfirmingLeave)
        #expect(RestoreModel.leaveQuestion(servers: 3) == "Your current account has 3 servers. They stay in that account. Continue?")
        #expect(restored == 0)

        await restore.continueLeaving()

        #expect(calls == [false, true])
        #expect(restored == 1)
        #expect(!restore.busy)
    }

    @Test func restoreFailureShowsAMessageAndCancelIsSilent() async {
        let failing = RestoreModel(restore: { _ in throw OIDCError.authorizationFailed("access_denied") }, startOver: {}, onRestored: {})
        await failing.signIn()
        #expect(failing.error == "The sign-in was refused.")

        let cancelled = RestoreModel(restore: { _ in throw CancellationError() }, startOver: {}, onRestored: {})
        await cancelled.signIn()
        #expect(cancelled.error == nil)
    }

    @Test func startOverRunsTheAccountStartOver() async {
        var started = 0
        let restore = RestoreModel(restore: { _ in }, startOver: { started += 1 }, onRestored: {})

        await restore.confirmStartOver()

        #expect(started == 1)
        #expect(RestoreModel.startOverQuestion == "Your old account stays on the Cloud. Start over?")
    }
}
