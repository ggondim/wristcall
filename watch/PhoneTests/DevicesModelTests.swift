import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct DevicesModelTests {
    private let phone = DeviceRecord(id: "dev_1", name: "Watch Ultra", createdAt: 1_000)
    private let spare = DeviceRecord(id: "dev_2", name: "Old Series 7", createdAt: 500)

    private func grant(expires: Double = 1_600, warning: String? = nil) -> PairingCodeGrant {
        PairingCodeGrant(code: "12345678", expiresAt: expires, serverUrl: "https://srv.test", viaDirectory: false, warning: warning)
    }

    @Test func loadShowsDevices() async {
        let fake = FakeServerAPI()
        fake.deviceList = [phone, spare]
        let model = DevicesModel(api: fake)
        await model.load()
        #expect(model.devices == [phone, spare])
        #expect(model.error == nil)
    }

    @Test func loadFailureKeepsListAndShowsMessage() async {
        let fake = FakeServerAPI()
        fake.deviceList = [phone]
        let model = DevicesModel(api: fake)
        await model.load()
        fake.devicesError = APIError.network(.notConnectedToInternet)
        await model.load()
        #expect(model.devices == [phone])
        #expect(model.error == APIError.network(.notConnectedToInternet).message)
    }

    @Test func revokeRemovesDevice() async {
        let fake = FakeServerAPI()
        fake.deviceList = [phone, spare]
        let model = DevicesModel(api: fake)
        await model.load()
        await model.revoke(phone)
        #expect(fake.revokedDevices == ["dev_1"])
        #expect(model.devices == [spare])
        #expect(model.error == nil)
    }

    @Test func revokeFailureKeepsDevice() async {
        let fake = FakeServerAPI()
        fake.deviceList = [phone]
        let model = DevicesModel(api: fake)
        await model.load()
        fake.revokeError = APIError.unavailable("The server is busy.")
        await model.revoke(phone)
        #expect(model.devices == [phone])
        #expect(model.error == "The server is busy.")
    }

    @Test func revokeOfVanishedDeviceDropsIt() async {
        let fake = FakeServerAPI()
        fake.deviceList = [phone]
        let model = DevicesModel(api: fake)
        await model.load()
        fake.revokeError = APIError.notFound
        await model.revoke(phone)
        #expect(model.devices.isEmpty)
        #expect(model.error == nil)
    }

    @Test func newPairingCodeShowsGrant() async {
        let fake = FakeServerAPI()
        fake.grant = grant(warning: "Directory unavailable.")
        let model = DevicesModel(api: fake, now: { Date(timeIntervalSince1970: 1_000) })
        await model.newPairingCode()
        #expect(model.code?.code == "12345678")
        #expect(model.code?.warning == "Directory unavailable.")
        #expect(model.error == nil)
    }

    @Test func codeExpiresFromScreen() async {
        let fake = FakeServerAPI()
        fake.grant = grant(expires: 1_600)
        var clock = Date(timeIntervalSince1970: 1_000)
        let model = DevicesModel(api: fake, now: { clock })
        await model.newPairingCode()
        #expect(model.code != nil)
        clock = Date(timeIntervalSince1970: 1_599)
        #expect(model.code != nil)
        clock = Date(timeIntervalSince1970: 1_600)
        #expect(model.code == nil)
    }

    @Test func alreadyExpiredGrantIsNotShown() async {
        let fake = FakeServerAPI()
        fake.grant = grant(expires: 900)
        let model = DevicesModel(api: fake, now: { Date(timeIntervalSince1970: 1_000) })
        await model.newPairingCode()
        #expect(model.code == nil)
    }

    @Test func deviceLimitShowsServerMessageAndNoCode() async {
        let fake = FakeServerAPI()
        fake.pairingCodeError = APIError.limit("You reached the limit of 5 devices.")
        let model = DevicesModel(api: fake)
        await model.newPairingCode()
        #expect(model.code == nil)
        #expect(model.error == "You reached the limit of 5 devices.")
    }

    @Test func failedNewCodeHidesThePreviousOne() async {
        let fake = FakeServerAPI()
        fake.grant = grant()
        let model = DevicesModel(api: fake, now: { Date(timeIntervalSince1970: 1_000) })
        await model.newPairingCode()
        fake.pairingCodeError = APIError.rateLimited
        await model.newPairingCode()
        #expect(model.code == nil)
        #expect(model.error == APIError.rateLimited.message)
    }

    @Test func errorsNeverCarryTheCode() async {
        let fake = FakeServerAPI()
        fake.grant = grant()
        let model = DevicesModel(api: fake, now: { Date(timeIntervalSince1970: 1_000) })
        await model.newPairingCode()
        #expect(!"\(model.code!)".contains("12345678"))
        #expect(!String(reflecting: model.code!).contains("12345678"))
    }

    @Test func codeIsShownInTwoGroupsOfFour() {
        #expect(PairingCodeView.grouped("12345678") == "1234 5678")
        #expect(PairingCodeView.grouped("1234") == "1234")
    }

    @Test func countdownShowsMinutesAndSeconds() {
        #expect(PairingCodeView.countdown(600) == "10:00")
        #expect(PairingCodeView.countdown(61) == "1:01")
        #expect(PairingCodeView.countdown(-3) == "0:00")
    }

    @Test func secondsLeftFollowsTheClock() async {
        let fake = FakeServerAPI()
        fake.grant = grant(expires: 1_600)
        var clock = Date(timeIntervalSince1970: 1_000)
        let model = DevicesModel(api: fake, now: { clock })
        #expect(model.secondsLeft() == 0)
        await model.newPairingCode()
        #expect(model.secondsLeft() == 600)
        clock = Date(timeIntervalSince1970: 1_599.5)
        #expect(model.secondsLeft() == 1)
    }
}
