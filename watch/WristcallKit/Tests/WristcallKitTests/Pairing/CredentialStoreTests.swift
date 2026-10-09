import Foundation
import Testing
import WristcallKit

struct CredentialStoreTests {
    let credentials = Credentials(
        serverURL: URL(string: "https://agent.example.com")!,
        deviceId: "dev-1",
        token: "secret-token"
    )

    @Test func credentialsFromAPairedDevice() {
        let paired = PairedDevice(deviceId: "dev-1", token: "secret-token")
        #expect(Credentials(serverURL: URL(string: "https://agent.example.com")!, device: paired, id: credentials.id) == credentials)
    }

    @Test func credentialsRoundTripThroughJSON() throws {
        let data = try JSONEncoder().encode(credentials)
        #expect(try JSONDecoder().decode(Credentials.self, from: data) == credentials)
    }

    @Test func credentialsDescriptionHidesTheToken() {
        for text in [String(describing: credentials), String(reflecting: credentials)] {
            #expect(!text.contains("secret-token"))
            #expect(text.contains("dev-1"))
        }
    }

    @Test func inMemoryStoreSavesLoadsAndDeletes() throws {
        let store = InMemoryCredentialStore()
        #expect(try store.load() == nil)
        try store.save(credentials)
        #expect(try store.load() == credentials)
        var replaced = credentials
        replaced.token = "new-token"
        try store.save(replaced)
        #expect(try store.load() == replaced)
        try store.delete()
        #expect(try store.load() == nil)
        try store.delete()
    }

    @Test func inMemoryStoreCanStartFilled() throws {
        #expect(try InMemoryCredentialStore(credentials).load() == credentials)
    }

    @Test func eachCredentialsGetsItsOwnId() {
        let other = Credentials(serverURL: credentials.serverURL, deviceId: "dev-1", token: "secret-token")
        #expect(other.id != credentials.id)
        #expect(!credentials.id.isEmpty)
    }

    @Test func theIdSurvivesJSON() throws {
        let kept = Credentials(serverURL: credentials.serverURL, deviceId: "dev-1", token: "t", id: "srv-1")
        let data = try JSONEncoder().encode(kept)
        #expect(try JSONDecoder().decode(Credentials.self, from: data).id == "srv-1")
    }

    @Test func aStoredItemWithoutIdFrom010GetsOne() throws {
        let json = Data(#"{"serverURL":"https://agent.example.com","deviceId":"dev-1","token":"t"}"#.utf8)
        let decoded = try JSONDecoder().decode(Credentials.self, from: json)
        #expect(!decoded.id.isEmpty)
        #expect(decoded.deviceId == "dev-1")
        let again = try JSONDecoder().decode(Credentials.self, from: json)
        #expect(again.id != decoded.id)
    }
}
