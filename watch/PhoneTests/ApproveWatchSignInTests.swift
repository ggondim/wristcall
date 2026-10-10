import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

/// Approving the watch's account login (R11): the iPhone opens `{issuer}/device?user_code=…` in its web sheet.
@MainActor
struct ApproveWatchSignInTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_800_000_000)

    let world = AccountWorld()

    func account(signedIn: Bool, web: any WebAuthenticator = FakeWeb(error: CancellationError())) async -> AccountModel {
        let tokens = signedIn
            ? InMemoryTokenStore(TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: Self.clock.addingTimeInterval(3600)))
            : InMemoryTokenStore()
        let session = AccountSession(cloud: world.cloud.url, kind: .ios, store: tokens, session: .stubbed(), now: { Self.clock })
        let model = AccountModel(cloudURL: world.cloud.url, session: session, web: web, state: AppState(store: InMemoryManagedServerStore()))
        await model.restore()
        return model
    }

    func link() -> WatchLink {
        WatchLink(session: nil, now: { Self.clock }, sleep: { _ in try await Task.sleep(for: .seconds(3600)) })
    }

    var deviceURL: String { world.issuer.url.absoluteString + "/device?user_code=ZXSG-KCPN" }

    // MARK: - The URL

    @Test func verificationURLUsesIssuer() {
        let issuer = URL(string: "https://auth.test")!
        #expect(DeviceVerification.url(issuer: issuer, userCode: "zxsg-kcpn")?.absoluteString == "https://auth.test/device?user_code=ZXSG-KCPN")
        #expect(DeviceVerification.url(issuer: issuer, userCode: " zxsgkcpn ")?.absoluteString == "https://auth.test/device?user_code=ZXSG-KCPN")
        #expect(DeviceVerification.url(issuer: URL(string: "https://auth.test/")!, userCode: "ZXSG-KCPN")?.absoluteString
            == "https://auth.test/device?user_code=ZXSG-KCPN")
    }

    @Test(arguments: ["x&y=1", "", "ZXSG-KCP", "ZXSG KCPN", "ZXSG-KCPN&x=1", "ZXSG/KCPN", "ZXSG-KCPÑ", "ZX-SGKCPN"])
    func verificationRejectsGarbage(code: String) {
        #expect(DeviceVerification.url(issuer: URL(string: "https://auth.test")!, userCode: code) == nil)
    }

    @Test func verificationRejectsInsecureIssuer() {
        #expect(DeviceVerification.url(issuer: URL(string: "http://auth.test")!, userCode: "ZXSG-KCPN") == nil)
        #expect(DeviceVerification.url(issuer: URL(string: "https://auth.test?x=1")!, userCode: "ZXSG-KCPN") == nil)
    }

    // MARK: - The sheet

    @Test func incomingCodeOpensSheetOnlySignedIn() async {
        let link = link()
        let signedOut = await account(signedIn: false)
        let signedIn = await account(signedIn: true)
        #expect(WatchSignInSheet.code(link: link, account: signedIn, dismissed: nil) == nil)
        link.receive(.deviceCode(userCode: "zxsg-kcpn", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        #expect(WatchSignInSheet.code(link: link, account: signedOut, dismissed: nil) == nil)
        #expect(WatchSignInSheet.code(link: link, account: signedIn, dismissed: nil) == "ZXSG-KCPN")
        // Closed by the user: the same code does not come back; a new one does.
        #expect(WatchSignInSheet.code(link: link, account: signedIn, dismissed: "ZXSG-KCPN") == nil)
        link.receive(.deviceCode(userCode: "ABCD-EFGH", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        #expect(WatchSignInSheet.code(link: link, account: signedIn, dismissed: "ZXSG-KCPN") == "ABCD-EFGH")
    }

    @Test func unavailableWithoutCloudShowsNoSheet() async {
        let link = link()
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        let model = AccountModel(cloudURL: nil, session: nil, web: FakeWeb(error: CancellationError()), state: AppState(store: InMemoryManagedServerStore()))
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: nil) == nil)
    }

    // MARK: - Approving

    @Test func approveOpensTheIssuersDevicePage() async throws {
        let web = FakeWeb(error: CancellationError())
        let model = await account(signedIn: true, web: web)
        await model.approveWatchSignIn(userCode: "zxsg-kcpn")
        let opened = try #require(web.opened.first)
        #expect(opened.url.absoluteString == deviceURL)
        #expect(opened.scheme == AccountModel.callbackScheme)
        // The page never comes back to the app: closing it ends the step.
        #expect(model.watchApproval == .done)
        #expect(AccountModel.checkWatch == "Check your watch.")
    }

    @Test func approveIgnoresBadCode() async {
        let web = FakeWeb(error: CancellationError())
        let model = await account(signedIn: true, web: web)
        await model.approveWatchSignIn(userCode: "x&y=1")
        #expect(web.opened.isEmpty)
        #expect(model.watchApproval == .failed(AccountModel.badUserCode))
    }

    @Test func approveNeedsTheAccount() async {
        let web = FakeWeb(error: CancellationError())
        let model = await account(signedIn: false, web: web)
        await model.approveWatchSignIn(userCode: "ZXSG-KCPN")
        #expect(web.opened.isEmpty)
        #expect(model.watchApproval == .idle)
    }

    @Test func approvePageThatFailsToOpen() async {
        let model = await account(signedIn: true, web: FakeWeb(error: WebAuthenticatorError.couldNotStart))
        await model.approveWatchSignIn(userCode: "ZXSG-KCPN")
        #expect(model.watchApproval == .failed("Can't open the sign-in page."))
    }

    @Test func watchSignedInClosesThePage() async throws {
        // M13: the `/device` page never redirects back; the watch's "signed in" closes it.
        let web = HangingWeb()
        let model = await account(signedIn: true, web: web)
        let link = link()
        link.onWatchSignedIn = { [weak model] in model?.watchSignInFinished() }
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        let running = Task { await model.approveWatchSignIn(userCode: "ZXSG-KCPN") }
        await waitFor { web.opened.count == 1 && model.watchApproval == .open }
        link.receive(.signedIn)
        await running.value
        #expect(web.wasCancelled)
        #expect(model.watchApproval == .done)
        #expect(link.incomingDeviceCode == nil)
    }

    @Test func watchSignedInClosesALivePageThatNeverCallsBack() async throws {
        // The real authenticator over a system session that never calls back: the M13 close still ends the
        // wait, and a second approval can start.
        let sessions = SessionBox()
        let web = LiveWebAuthenticator { _, _, _ in
            let session = SilentWebSession()
            sessions.all.append(session)
            return session
        }
        let model = await account(signedIn: true, web: web)
        let running = Task { await model.approveWatchSignIn(userCode: "ZXSG-KCPN") }
        await waitFor { sessions.all.count == 1 && model.watchApproval == .open }
        model.watchSignInFinished()
        await running.value
        #expect(model.watchApproval == .done)
        #expect(sessions.all.first?.cancelled == 1)
        let again = Task { await model.approveWatchSignIn(userCode: "ZXSG-KCPN") }
        await waitFor { sessions.all.count == 2 }
        #expect(model.watchApproval == .open)
        model.watchSignInFinished()
        await again.value
    }

    @Test func sheetStaysWhileThePageIsOpen() async throws {
        // The code expires (or the watch signs in) while the page is up: the sheet it was opened from stays
        // until the page closes, so the page is never left without its sheet.
        let web = HangingWeb()
        let model = await account(signedIn: true, web: web)
        let link = link()
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        let running = Task { await model.approveWatchSignIn(userCode: "ZXSG-KCPN", from: .sheet) }
        await waitFor { model.watchApproval == .open }
        #expect(model.watchApprovalOrigin == .sheet)
        link.receive(.signedIn)
        #expect(link.incomingDeviceCode == nil)
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: "ZXSG-KCPN") == "ZXSG-KCPN")
        model.watchSignInFinished()
        await running.value
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: nil) == nil)
    }

    @Test func settingsApprovalNeverShowsTheSheet() async throws {
        // Approved from Settings (typed code): no sheet pops over Settings while the page opens, nor for that
        // code once the page closed.
        let web = HangingWeb()
        let model = await account(signedIn: true, web: web)
        let link = link()
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        let running = Task { await model.approveWatchSignIn(userCode: "zxsg-kcpn", from: .settings) }
        await waitFor { model.watchApproval == .open }
        #expect(model.watchApprovalOrigin == .settings)
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: nil) == nil)
        model.watchSignInFinished()
        await running.value
        #expect(model.watchApproval == .done)
        #expect(model.watchApprovalOrigin == nil)
        #expect(link.incomingDeviceCode == "ZXSG-KCPN")
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: nil) == nil)
        // Another code from the watch still brings the sheet.
        link.receive(.deviceCode(userCode: "ABCD-EFGH", expiresAt: Self.clock.timeIntervalSince1970 + 600))
        #expect(WatchSignInSheet.code(link: link, account: model, dismissed: nil) == "ABCD-EFGH")
    }

    @Test func approvalDefaultsToSettings() async throws {
        // An approval that does not say where it came from never keeps a sheet up.
        let web = HangingWeb()
        let model = await account(signedIn: true, web: web)
        let running = Task { await model.approveWatchSignIn(userCode: "ZXSG-KCPN") }
        await waitFor { model.watchApproval == .open }
        #expect(model.watchApprovalOrigin == .settings)
        #expect(WatchSignInSheet.code(link: link(), account: model, dismissed: nil) == nil)
        model.watchSignInFinished()
        await running.value
    }

    @Test func watchSignedInWithoutAPageDoesNothing() async {
        let model = await account(signedIn: true)
        model.watchSignInFinished()
        #expect(model.watchApproval == .idle)
    }
}

/// A page that stays open until its task is cancelled (the `/device` page, which never redirects back).
@MainActor
final class HangingWeb: WebAuthenticator {
    private(set) var opened: [URL] = []
    private(set) var wasCancelled = false

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        opened.append(url)
        do {
            try await Task.sleep(for: .seconds(30))
        } catch {
            wasCancelled = true
            throw error
        }
        return url
    }
}

/// Polls `condition` on the main actor every 10 ms, at most 2 s.
@MainActor
private func waitFor(_ condition: () -> Bool) async {
    for _ in 0..<200 where !condition() {
        try? await Task.sleep(for: .milliseconds(10))
    }
}
