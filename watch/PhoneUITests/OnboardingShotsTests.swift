import XCTest

/// Captures the iPhone app v3 screens as PNGs (welcome, plan, agents, new agent, link confirm, settings) for
/// the docs and the store listing. Not a regression test: it only looks at the screens and never taps
/// anything that changes an account.
///
/// Runs only through the `WristcallPhoneUI` scheme (`make -C watch shots-ios`), never in CI, and skips
/// without `WRISTCALL_UI_SHOTS`. Variables reach the test process with the `TEST_RUNNER_` prefix:
///
///     TEST_RUNNER_WRISTCALL_UI_SHOTS=/tmp/shots make -C watch shots-ios
///
/// - `WRISTCALL_UI_SHOTS` (folder, required): where `iphone-<nn>-<name>.png` are written.
/// - `WRISTCALL_UI_SERVER` + `WRISTCALL_UI_SERVER_TOKEN` (a server URL and a personal token of it): add the
///   "agents" and "new agent" screens. Those cases skip without both.
/// - `WRISTCALL_UI_LINK` (a `wristcall://` link, or the https link of a QR code): adds "link confirm".
/// - `WRISTCALL_UI_INVITE` (an unused Wristcall Cloud invite code): adds the Cloud entry cases (`test08` to
///   `test12`). They need a Cloud with the entry turned on for this build and a node registered on it (a local
///   one: `CLOUD_URL=http://127.0.0.1:8090`). `test08` redeems the code during onboarding and works with or
///   without a node up (with none, the Cloud section says the Cloud is unavailable); the others need the node
///   up and the same installation. `test09` shows the 80 % warning once the month's usage is there, and
///   `test12` needs a call saved to the History (one-shot Voice notes call from the watch, say). Run them one
///   at a time with `-only-testing:WristcallPhoneUITests/OnboardingShotsTests/<case>` when the stack changes
///   between them.
///
/// The cases run in a fixed order (their names are numbered) and share ONE installed app: the app is never
/// uninstalled between cases, so a single silent account is created on the Cloud per run (there is a limit
/// of 3 accounts an hour per IP). The first case starts from `-resetOnboarding`, and the cases that need an
/// app without servers run before any server is added. Start the run on a simulator without the app's
/// account and servers (`SHOTS_UDID=<udid> SHOTS_RESET=1 make shots-ios` resets one).
@MainActor
final class OnboardingShotsTests: XCTestCase {
    private var app: XCUIApplication!
    private var shotsFolder: URL!
    private var server = ""
    private var serverToken = ""
    private var link = ""
    private var invite = ""
    private let timeout: TimeInterval = 20
    private static let cloudUnavailable = "Wristcall Cloud is temporarily unavailable. Try again in a minute."

    override func setUpWithError() throws {
        continueAfterFailure = false
        let env = ProcessInfo.processInfo.environment
        let folder = env["WRISTCALL_UI_SHOTS"] ?? ""
        try XCTSkipIf(folder.isEmpty, "set TEST_RUNNER_WRISTCALL_UI_SHOTS=<folder> to capture the screens")
        shotsFolder = URL(fileURLWithPath: folder, isDirectory: true)
        try FileManager.default.createDirectory(at: shotsFolder, withIntermediateDirectories: true)
        server = env["WRISTCALL_UI_SERVER"] ?? ""
        serverToken = env["WRISTCALL_UI_SERVER_TOKEN"] ?? ""
        link = env["WRISTCALL_UI_LINK"] ?? ""
        invite = env["WRISTCALL_UI_INVITE"] ?? ""
        app = XCUIApplication()
    }

    override func tearDownWithError() throws {
        app?.terminate()
    }

    // MARK: - Cases, in order

    func test01Welcome() throws {
        launch("-resetOnboarding")
        XCTAssertTrue(app.buttons["Get started"].waitForExistence(timeout: timeout), "no welcome screen")
        shot("iphone-01-welcome")
    }

    func test02Plan() throws {
        launch("-resetOnboarding", "-debugOpen", "plan")
        XCTAssertTrue(app.staticTexts["How do you want to start?"].waitForExistence(timeout: timeout), "no plan screen")
        settle()
        shot("iphone-02-plan")
    }

    /// Welcome, then "Get started", then "Not now": the app opens on the agents with no server.
    func test03AgentsEmpty() throws {
        launch("-resetOnboarding")
        let start = app.buttons["Get started"]
        XCTAssertTrue(start.waitForExistence(timeout: timeout), "no welcome screen")
        start.tap()
        let notNow = app.buttons["Not now"]
        XCTAssertTrue(notNow.waitForExistence(timeout: timeout), "no plan screen")
        notNow.tap()
        XCTAssertTrue(
            app.staticTexts["Connect your server"].waitForExistence(timeout: timeout),
            "no empty agents screen (does the simulator still have servers? reset its Keychain)"
        )
        settle()
        shot("iphone-04-agents-empty")
    }

    func test04Settings() throws {
        launch("-debugOpen", "settings")
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: timeout), "no Settings screen")
        settle(seconds: 2) // the account row fills in once the Cloud answers
        shot("iphone-08-settings")
    }

    func test05LinkConfirm() throws {
        try XCTSkipIf(link.isEmpty, "set TEST_RUNNER_WRISTCALL_UI_LINK to capture the link screen")
        launch("-debugOpenURL", link)
        XCTAssertTrue(app.buttons["Connect"].waitForExistence(timeout: timeout), "no link confirm screen")
        settle(seconds: 2)
        shot("iphone-07-link-confirm")
    }

    func test06Agents() throws {
        try requireServer()
        launch("-addServer", server, "-addServerToken", serverToken, "-addServerName", "Shots server")
        waitForAgents()
        shot("iphone-03-agents")
    }

    func test07NewAgent() throws {
        try requireServer()
        launch("-addServer", server, "-addServerToken", serverToken, "-addServerName", "Shots server",
               "-debugOpen", "newAgent")
        XCTAssertTrue(app.buttons["Save"].waitForExistence(timeout: timeout), "no agent editor")
        settle()
        shot("iphone-05-new-agent")
    }

    // MARK: - Wristcall Cloud entry

    /// Welcome, "Get started", then "Enter invite code" with `WRISTCALL_UI_INVITE`: the app opens on the agents
    /// with the Cloud entry's Voice notes from the agenda (and, with no node up, the line saying so).
    func test08CloudInvite() throws {
        try requireInvite()
        launch("-resetOnboarding")
        tap(app.buttons["Get started"], "no welcome screen")
        tap(app.buttons["Enter invite code"], "no plan screen (is the Cloud set for this build?)")
        let field = app.textFields["Invite code"].exists ? app.textFields["Invite code"] : app.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: timeout), "no invite code sheet")
        field.tap()
        field.typeText(invite)
        let redeem = app.buttons["Redeem"]
        XCTAssertTrue(redeem.waitForExistence(timeout: timeout), "no Redeem button (is the account still being made?)")
        waitUntilEnabled(redeem, "Redeem never enabled")
        redeem.tap()
        dismissSystemAlerts()
        waitForCloudAgents()
        // No node up: the section's error line comes once the Cloud's route answer is in.
        _ = app.staticTexts[Self.cloudUnavailable].waitForExistence(timeout: 10)
        settle(seconds: 2)
        shot("iphone-09-cloud-agents")
    }

    /// The agents with a node up: Voice notes without the unavailable line, and the usage warning when the month
    /// is at 80 % or more.
    func test09CloudAgents() throws {
        try requireInvite()
        launch()
        dismissSystemAlerts()
        waitForCloudAgents()
        let warning = element(containing: "of this month's call minutes")
        print("usage warning shown: \(warning.waitForExistence(timeout: 10))")
        XCTAssertFalse(app.staticTexts[Self.cloudUnavailable].exists, "the Cloud entry is unavailable (no node up?)")
        settle(seconds: 2)
        shot("iphone-10-cloud-agents-node")
    }

    /// Settings: the plan row with the month's usage line.
    func test10CloudPlan() throws {
        try requireInvite()
        launch("-debugOpen", "settings")
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: timeout), "no Settings screen")
        XCTAssertTrue(element(containing: "min used this month").waitForExistence(timeout: timeout),
                      "no usage line on the plan row")
        settle(seconds: 2)
        shot("iphone-11-cloud-plan")
    }

    /// The Voice notes editor (from the node): its webhook is "None, save to History".
    func test11CloudVoiceNotesEditor() throws {
        try requireInvite()
        launch()
        dismissSystemAlerts()
        waitForCloudAgents()
        app.staticTexts["Voice notes"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Voice notes"].waitForExistence(timeout: timeout), "no Voice notes editor")
        let none = element(containing: "None, save to History")
        var swipes = 0
        while !none.waitForExistence(timeout: swipes == 0 ? timeout : 2) && swipes < 4 {
            app.swipeUp()
            swipes += 1
        }
        XCTAssertTrue(none.exists, "the webhook is not \"None, save to History\"")
        settle(seconds: 2)
        shot("iphone-12-cloud-voice-notes")
    }

    /// History with a call the node kept in the History only: "Saved".
    func test12CloudHistorySaved() throws {
        try requireInvite()
        launch("-debugOpen", "history")
        XCTAssertTrue(app.navigationBars["History"].waitForExistence(timeout: timeout), "no History screen")
        XCTAssertTrue(element(containing: "Saved").waitForExistence(timeout: timeout), "no saved call")
        settle(seconds: 2)
        shot("iphone-13-cloud-history-saved")
    }

    // MARK: - Helpers

    private func requireInvite() throws {
        try XCTSkipIf(invite.isEmpty, "set TEST_RUNNER_WRISTCALL_UI_INVITE to capture the Wristcall Cloud entry")
    }

    /// The agents tab with the Cloud entry: its section header and the Voice notes agent from the agenda.
    private func waitForCloudAgents() {
        XCTAssertTrue(app.staticTexts["Voice notes"].firstMatch.waitForExistence(timeout: 30), "no Voice notes agent")
        XCTAssertTrue(element(containing: "WRISTCALL CLOUD").exists, "no Wristcall Cloud section")
    }

    private func tap(_ element: XCUIElement, _ message: String) {
        XCTAssertTrue(element.waitForExistence(timeout: timeout), message)
        element.tap()
    }

    private func waitUntilEnabled(_ element: XCUIElement, _ message: String) {
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isEnabled == true"), object: element)
        XCTAssertEqual(XCTWaiter().wait(for: [enabled], timeout: timeout), .completed, message)
    }

    /// The first element whose label contains `text`.
    private func element(containing text: String) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", text)).firstMatch
    }

    /// The notifications prompt the first launches may show.
    private func dismissSystemAlerts() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for label in ["Allow", "OK"] where springboard.alerts.buttons[label].waitForExistence(timeout: 2) {
            springboard.alerts.buttons[label].tap()
            return
        }
    }

    private func requireServer() throws {
        try XCTSkipIf(
            server.isEmpty || serverToken.isEmpty,
            "set TEST_RUNNER_WRISTCALL_UI_SERVER and TEST_RUNNER_WRISTCALL_UI_SERVER_TOKEN to capture the agents"
        )
    }

    /// Relaunches the app (same installation, same account) with the given launch arguments.
    private func launch(_ arguments: String...) {
        app.terminate()
        app.launchArguments = arguments
        app.launch()
    }

    /// The agents tab with a server: "New agent" enabled means the list has loaded.
    private func waitForAgents() {
        let newAgent = app.buttons["New agent"]
        XCTAssertTrue(newAgent.waitForExistence(timeout: timeout), "no agents screen")
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isEnabled == true"), object: newAgent)
        XCTAssertEqual(XCTWaiter().wait(for: [enabled], timeout: timeout), .completed, "the agents never loaded")
        settle(seconds: 2)
    }

    /// Lets animations and the first network answers finish before the picture.
    private func settle(seconds: TimeInterval = 1) {
        _ = XCTWaiter().wait(for: [XCTestExpectation(description: "settle")], timeout: seconds)
    }

    private func shot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        let file = shotsFolder.appendingPathComponent("\(name).png")
        do {
            try screenshot.pngRepresentation.write(to: file)
        } catch {
            XCTFail("could not write \(file.lastPathComponent): \(error.localizedDescription)")
        }
    }
}
