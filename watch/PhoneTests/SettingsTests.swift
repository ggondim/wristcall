import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct SettingsTests {
    let world = AccountWorld()

    private func device(
        _ id: String, name: String = "Device", platform: String = "ios", current: Bool = false,
        lastSeen: Double? = nil
    ) -> String {
        let seen = lastSeen.map { #","last_seen_at":\#($0)"# } ?? ""
        return #"{"id":"\#(id)","name":"\#(name)","platform":"\#(platform)","created_at":1799990000\#(seen),"current":\#(current)}"#
    }

    private func makeRig(hasKey: Bool = true, account: AccountWorld.Account = AccountWorld.Account()) async -> AccountRig {
        let key = StoredAccountKey.of("acc_1", age: 60)
        world.addAccount("acc_1", key: key.key, account)
        let rig = AccountRig(world, session: world.storedSession("acc_1"), keys: hasKey ? [key] : [])
        _ = await rig.started()
        return rig
    }

    // MARK: - Rows

    @Test func useOnTheWebWaitsForTheWebSignIn() {
        #expect(WEB_SIGN_IN_READY == false)
        #expect(SettingsContent.accountRows() == [.devices])
        #expect(SettingsContent.accountRows(webSignInReady: false) == [.devices])
        #expect(SettingsContent.accountRows(webSignInReady: true) == [.useOnTheWeb, .devices])
    }

    @Test func planRowReadsTheAccountPlan() async {
        #expect(SettingsContent.planRow(for: Plan(plan: .full, source: "beta"))
            == .init(title: "Wristcall Cloud", detail: "Beta · invite"))
        #expect(SettingsContent.planRow(for: Plan(plan: .full, source: "invite"))
            == .init(title: "Wristcall Cloud", detail: "Beta · invite"))
        #expect(SettingsContent.planRow(for: Plan(plan: .custom, source: "beta"))
            == .init(title: "Cloud with your models", detail: "Beta · invite"))
        #expect(SettingsContent.planRow(for: Plan(plan: .full, source: "partner"))
            == .init(title: "Wristcall Cloud", detail: "Beta · partner"))
        #expect(SettingsContent.planRow(for: nil) == .init(title: "No plan", detail: "Choose how to start"))

        let custom = await makeRig(account: AccountWorld.Account(plan: #"{"plan":"custom","source":"beta"}"#))
        #expect(SettingsContent.planRow(for: custom.model.plan).title == "Cloud with your models")
        let none = await makeRig()
        #expect(SettingsContent.planRow(for: none.model.plan).title == "No plan")
    }

    @Test func anUnknownPlanReadsAsCloudWithItsName() async {
        #expect(SettingsContent.planRow(for: Plan(plan: .other("team"), source: "invite"))
            == .init(title: "Wristcall Cloud", detail: "team"))
        // The account still reads with a plan this version does not know.
        let rig = await makeRig(account: AccountWorld.Account(plan: #"{"plan":"team","source":"invite"}"#))
        #expect(rig.model.state == .ready)
        #expect(rig.model.plan == Plan(plan: .other("team"), source: "invite"))
        #expect(SettingsContent.planRow(for: rig.model.plan) == .init(title: "Wristcall Cloud", detail: "team"))
    }

    @Test func anApprovalNotificationOpensTheServerFromServers() {
        #expect(SettingsRoute.path(opening: "srv-1") == [.servers, .server("srv-1")])
    }

    @Test func deleteCopyIsTheMockups() {
        #expect(SettingsContent.deleteExplanation == "Delete your wristcall account? This deletes your account, the agents "
            + "list in Wristcall Cloud and every signed-in device. Servers you run keep their data.")
        #expect(AccountDevicesModel.signOutEverywhereMessage
            == "Sign out every other device? They need iCloud Keychain or a new sign-in to come back.")
    }

    // MARK: - Devices

    @Test func signOutEverywhereNeedsALocalKey() async {
        let keyless = await makeRig(hasKey: false)
        let without = AccountDevicesModel(account: keyless.model)
        #expect(!without.offersSignOutEverywhere)
        await without.signOutEverywhere()
        #expect(world.cloudRequests("POST", "/v1/account/key/rotate").isEmpty)

        let keyed = await makeRig()
        let with = AccountDevicesModel(account: keyed.model)
        #expect(with.offersSignOutEverywhere)
        world.devicesJSON = #"{"devices":[\#(device("dev_0", current: true))]}"#
        await with.signOutEverywhere()
        #expect(world.cloudRequests("POST", "/v1/account/key/rotate").count == 1)
        #expect(with.notice == "Signed out 2 other devices.")
        #expect(with.devices.map(\.id) == ["dev_0"])
        #expect(with.error == nil)
    }

    @Test func revokeIsNotOfferedForThisDevice() async {
        let here = AccountDevice(id: "dev_0", name: "iPhone", platform: "ios", createdAt: .now, current: true)
        let other = AccountDevice(id: "dev_1", name: "Watch", platform: "watchos", createdAt: .now, current: false)
        #expect(!AccountDevicesModel.canRevoke(here))
        #expect(AccountDevicesModel.canRevoke(other))

        let rig = await makeRig()
        world.devicesJSON = #"{"devices":[\#(device("dev_1", name: "Watch", platform: "watchos")),\#(device("dev_0", current: true))]}"#
        let model = AccountDevicesModel(account: rig.model)
        await model.load()
        #expect(model.devices.map(\.id) == ["dev_0", "dev_1"])
        await model.revoke(model.devices[0])
        #expect(world.cloudRequests("DELETE", "/v1/devices/dev_0").isEmpty)
        #expect(model.devices.count == 2)
        await model.revoke(model.devices[1])
        #expect(world.cloudRequests("DELETE", "/v1/devices/dev_1").count == 1)
        #expect(model.devices.map(\.id) == ["dev_0"])
    }

    @Test func aFailedListShowsTheMessage() async {
        let rig = await makeRig()
        world.cloudFailure = 503
        let model = AccountDevicesModel(account: rig.model)
        await model.load()
        #expect(model.error != nil)
        #expect(model.didLoad)
    }

    // MARK: - Text

    @Test func deviceNamesFitOneLine() {
        #expect(AccountDevicesModel.oneLine("Ana\u{2028}iPhone\u{2029}Pro") == "Ana iPhone Pro")
        #expect(AccountDevicesModel.oneLine("  Ana's \n iPhone\t") == "Ana's iPhone")
        #expect(AccountDevicesModel.oneLine("\u{2028}") == "Device")
        #expect(AccountDevicesModel.oneLine("Ana's iPhone") == "Ana's iPhone")
    }

    @Test func subtitlesNameThePlatformAndTheLastUse() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let here = AccountDevice(id: "d", name: "n", platform: "ios", createdAt: now, current: true)
        #expect(AccountDevicesModel.subtitle(for: here, now: now) == "This iPhone")
        let watch = AccountDevice(
            id: "w", name: "n", platform: "watchos", createdAt: now.addingTimeInterval(-86_400 * 9),
            lastSeenAt: now.addingTimeInterval(-3 * 3600), current: false)
        #expect(AccountDevicesModel.subtitle(for: watch, now: now) == "Apple Watch · last used 3 hours ago")
        let web = AccountDevice(id: "x", name: "n", platform: "web", createdAt: now.addingTimeInterval(-2 * 86_400), current: false)
        #expect(AccountDevicesModel.subtitle(for: web, now: now).hasPrefix("Web · added "))
        #expect(AccountDevicesModel.platformName("visionos") == "visionos")
    }

    @Test func signOutNoticeCountsTheDevices() {
        #expect(AccountDevicesModel.signedOutNotice(0) == "No other device was signed in.")
        #expect(AccountDevicesModel.signedOutNotice(1) == "Signed out 1 other device.")
        #expect(AccountDevicesModel.signedOutNotice(3) == "Signed out 3 other devices.")
    }
}
