import XCTest

/// Smoke test of the iPhone app against a running test server (`make -C watch test-server`): add a server,
/// open its agents, create a one-shot agent, open History and Settings, then remove the server again (so a rerun
/// starts from the same state; a failed run may leave it, and adding the same address again just replaces it).
/// Every screen is attached as a screenshot (`XCTAttachment`, kept in the result bundle) and, when
/// `WRISTCALL_UI_SHOTS` names a folder, also written there as `t12-<name>.png`.
///
/// Runs only through the `WristcallPhoneUI` scheme (`make -C watch test-ios-ui`), never in CI. Variables reach the
/// test process with the `TEST_RUNNER_` prefix:
///
///     TEST_RUNNER_WRISTCALL_UI_TOKEN=wc_pat_... make -C watch test-ios-ui
///
/// `WRISTCALL_UI_TOKEN` (a personal token of the test server) is required, the test skips without it;
/// `WRISTCALL_UI_SERVER` defaults to `http://127.0.0.1:8765`.
@MainActor
final class PhoneSmokeTests: XCTestCase {
    private var app: XCUIApplication!
    private var server = "http://127.0.0.1:8765"
    private var token = ""
    private var slug = ""
    private var shotsFolder: URL?

    override func setUpWithError() throws {
        continueAfterFailure = false
        let env = ProcessInfo.processInfo.environment
        token = env["WRISTCALL_UI_TOKEN"] ?? ""
        try XCTSkipIf(token.isEmpty, "set TEST_RUNNER_WRISTCALL_UI_TOKEN (a personal token of the test server)")
        if let value = env["WRISTCALL_UI_SERVER"], !value.isEmpty { server = value }
        if let folder = env["WRISTCALL_UI_SHOTS"], !folder.isEmpty {
            shotsFolder = URL(fileURLWithPath: folder, isDirectory: true)
            try? FileManager.default.createDirectory(at: shotsFolder!, withIntermediateDirectories: true)
        }
        // A slug of its own per run: the test only touches the agent it created.
        slug = "ui-\(UUID().uuidString.prefix(8).lowercased())"
        app = XCUIApplication()
        app.launch()
    }

    override func tearDown() {
        guard !token.isEmpty, !slug.isEmpty, let url = URL(string: server + "/v1/agents/" + slug) else { return }
        // Removes the agent when the test stopped before deleting it (a missing one is not an error).
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in done.signal() }.resume()
        _ = done.wait(timeout: .now() + 10)
    }

    func testAddServerCreateAgentHistorySettings() throws {
        let timeout: TimeInterval = 15

        // 1. Add the server.
        XCTAssertTrue(app.navigationBars["Servers"].waitForExistence(timeout: timeout))
        app.buttons["Add server"].firstMatch.tap()
        let address = app.textFields["Address (https://…)"]
        XCTAssertTrue(address.waitForExistence(timeout: timeout))
        address.tap()
        address.typeText(server)
        let tokenField = app.secureTextFields["Personal token (wc_pat_…)"]
        tokenField.tap()
        tokenField.typeText(token)
        let name = app.textFields["Name (optional)"]
        name.tap()
        name.typeText("Smoke server")
        shot("1-add-server")
        app.buttons["Add"].tap()
        let row = app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "Smoke server")).firstMatch
        if !row.waitForExistence(timeout: timeout) { shot("failure-add-server") }
        XCTAssertTrue(row.exists, "the server row never appeared")
        XCTAssertFalse(app.navigationBars["Add server"].exists)
        shot("2-servers")

        // 2. Its agents.
        row.tap()
        let agentsLink = app.buttons["Agents"].firstMatch
        XCTAssertTrue(agentsLink.waitForExistence(timeout: timeout))
        shot("3-server")
        agentsLink.tap()
        XCTAssertTrue(app.navigationBars["Agents"].waitForExistence(timeout: timeout))
        let newAgent = app.buttons["New agent"]
        XCTAssertTrue(newAgent.waitForExistence(timeout: timeout))
        // The button stays disabled until the providers are loaded.
        let enabled = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isEnabled == true"), object: newAgent)
        XCTAssertEqual(XCTWaiter().wait(for: [enabled], timeout: timeout), .completed, "the providers never loaded")
        shot("4-agents")

        // 3. A one-shot agent with a webhook.
        newAgent.tap()
        let nameField = app.textFields["Name"]
        XCTAssertTrue(nameField.waitForExistence(timeout: timeout))
        nameField.tap()
        nameField.typeText("UI smoke")
        let slugField = app.textFields["Slug"]
        slugField.tap()
        slugField.press(forDuration: 1.0)
        if app.menuItems["Select All"].waitForExistence(timeout: 2) { app.menuItems["Select All"].tap() }
        slugField.typeText(slug)
        pick("Call type", "One-shot")
        // The webhook stage is where the one-shot agent differs: "Custom" shows the address field.
        pick("Webhook", "Custom")
        let hook = app.textFields["https://…"]
        XCTAssertTrue(hook.waitForExistence(timeout: timeout))
        hook.tap()
        hook.typeText("https://example.com/hook")
        shot("5-new-agent")
        let save = app.buttons["Save"]
        XCTAssertTrue(save.isEnabled, "Save is disabled: the form reports a problem")
        save.tap()
        let created = app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "UI smoke")).firstMatch
        XCTAssertTrue(created.waitForExistence(timeout: timeout), "the new agent never showed in the list")
        shot("6-agent-created")

        // 4. History and Settings.
        app.tabBars.buttons["History"].tap()
        XCTAssertTrue(app.navigationBars["History"].waitForExistence(timeout: timeout))
        shot("7-history")
        app.tabBars.buttons["Settings"].tap()
        XCTAssertTrue(app.navigationBars["Settings"].waitForExistence(timeout: timeout))
        XCTAssertTrue(app.staticTexts["Version"].exists)
        shot("8-settings")

        // 5. Remove the server again, so a rerun starts from the same state.
        app.tabBars.buttons["Servers"].tap()
        let added = app.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH %@", "Smoke server")).firstMatch
        XCTAssertTrue(added.waitForExistence(timeout: timeout))
        added.tap()
        let remove = app.buttons["Remove server"].firstMatch
        XCTAssertTrue(remove.waitForExistence(timeout: timeout))
        remove.tap()
        // On iPhone the confirmation is a popover (a sheet in other layouts); it repeats the button's label.
        let popoverButton = app.popovers.buttons["Remove server"].firstMatch
        let sheetButton = app.sheets.buttons["Remove server"].firstMatch
        if popoverButton.waitForExistence(timeout: 5) {
            popoverButton.tap()
        } else if sheetButton.waitForExistence(timeout: 5) {
            sheetButton.tap()
        } else {
            XCTFail("no confirmation for removing the server")
        }
        XCTAssertTrue(app.navigationBars["Servers"].waitForExistence(timeout: timeout))
        let gone = added.waitForNonExistence(timeout: timeout)
        shot("10-server-removed")
        XCTAssertTrue(gone, "the server was not removed")
    }

    // MARK: Helpers

    /// Opens a menu picker whose row starts with `title` and chooses `option`.
    private func pick(_ title: String, _ option: String) {
        let picker = app.buttons.matching(NSPredicate(format: "label BEGINSWITH %@", title)).firstMatch
        XCTAssertTrue(picker.waitForExistence(timeout: 10), "no picker \(title)")
        picker.tap()
        let choice = app.buttons[option].firstMatch
        XCTAssertTrue(choice.waitForExistence(timeout: 10), "no option \(option) in \(title)")
        choice.tap()
    }

    private func shot(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let folder = shotsFolder {
            try? screenshot.pngRepresentation.write(to: folder.appendingPathComponent("t12-\(name).png"))
        }
    }
}
