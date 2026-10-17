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
/// - `WRISTCALL_UI_SHOTS` (folder, required): where `o5-<nn>-<name>.png` are written.
/// - `WRISTCALL_UI_SERVER` + `WRISTCALL_UI_SERVER_TOKEN` (a server URL and a personal token of it): add the
///   "agents" and "new agent" screens. Those cases skip without both.
/// - `WRISTCALL_UI_LINK` (a `wristcall://` link, or the https link of a QR code): adds "link confirm".
///
/// The cases run in a fixed order (their names are numbered) and share ONE installed app: the app is never
/// uninstalled between cases, so a single silent account is created on the Cloud per run (there is a limit
/// of 3 accounts an hour per IP). The first case starts from `-resetOnboarding`, and the cases that need an
/// app without servers run before any server is added. Start the run on a simulator without the app's
/// servers: `make shots-ios` resets the simulator's Keychain for that.
@MainActor
final class OnboardingShotsTests: XCTestCase {
    private var app: XCUIApplication!
    private var shotsFolder: URL!
    private var server = ""
    private var serverToken = ""
    private var link = ""
    private let timeout: TimeInterval = 20

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
        app = XCUIApplication()
    }

    override func tearDownWithError() throws {
        app?.terminate()
    }

    // MARK: - Cases, in order

    func test01Welcome() throws {
        launch("-resetOnboarding")
        XCTAssertTrue(app.buttons["Get started"].waitForExistence(timeout: timeout), "no welcome screen")
        shot("o5-01-welcome")
    }

    func test02Plan() throws {
        launch("-resetOnboarding", "-debugOpen", "plan")
        XCTAssertTrue(app.staticTexts["How do you want to start?"].waitForExistence(timeout: timeout), "no plan screen")
        settle()
        shot("o5-02-plan")
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
        shot("o5-04-agents-empty")
    }

    func test04Settings() throws {
        launch("-debugOpen", "settings")
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: timeout), "no Settings screen")
        settle(seconds: 2) // the account row fills in once the Cloud answers
        shot("o5-08-settings")
    }

    func test05LinkConfirm() throws {
        try XCTSkipIf(link.isEmpty, "set TEST_RUNNER_WRISTCALL_UI_LINK to capture the link screen")
        launch("-debugOpenURL", link)
        XCTAssertTrue(app.buttons["Connect"].waitForExistence(timeout: timeout), "no link confirm screen")
        settle(seconds: 2)
        shot("o5-07-link-confirm")
    }

    func test06Agents() throws {
        try requireServer()
        launch("-addServer", server, "-addServerToken", serverToken, "-addServerName", "Shots server")
        waitForAgents()
        shot("o5-03-agents")
    }

    func test07NewAgent() throws {
        try requireServer()
        launch("-addServer", server, "-addServerToken", serverToken, "-addServerName", "Shots server",
               "-debugOpen", "newAgent")
        XCTAssertTrue(app.buttons["Save"].waitForExistence(timeout: timeout), "no agent editor")
        settle()
        shot("o5-05-new-agent")
    }

    // MARK: - Helpers

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
