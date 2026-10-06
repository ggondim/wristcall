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
        #expect(Credentials(serverURL: URL(string: "https://agent.example.com")!, device: paired) == credentials)
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
}
