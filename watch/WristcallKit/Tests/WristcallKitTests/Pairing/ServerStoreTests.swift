import Foundation
import Testing
import WristcallKit

struct ServerStoreTests {
    let first = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "t1", id: "srv-1")
    let second = Credentials(
        serverURL: URL(string: "https://home.example.com")!, deviceId: "dev-2", token: "t2", id: "srv-2")

    @Test func inMemoryStoreSavesLoadsAndDeletes() throws {
        let store = InMemoryServerStore()
        #expect(try store.load().isEmpty)
        try store.save([first, second])
        #expect(try store.load() == [first, second])
        try store.save([second])
        #expect(try store.load() == [second])
        try store.deleteAll()
        #expect(try store.load().isEmpty)
        try store.deleteAll()
    }

    @Test func inMemoryStoreCanStartFilled() throws {
        #expect(try InMemoryServerStore([first]).load() == [first])
    }
}
