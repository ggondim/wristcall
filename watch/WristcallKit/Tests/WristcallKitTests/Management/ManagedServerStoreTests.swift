import Foundation
import Testing
import WristcallKit

struct ManagedServerStoreTests {
    let first = ManagedServer(
        id: "srv-1", name: "Home", url: URL(string: "https://home.example.com")!, token: "wc_pat_secret1")
    let second = ManagedServer(
        id: "srv-2", name: "Work", url: URL(string: "https://work.example.com")!, token: "wc_pat_secret2",
        cloudServerID: "cloud-9", linked: true)

    @Test func roundTrip() throws {
        let store = InMemoryManagedServerStore()
        #expect(try store.load().isEmpty)
        try store.save([first, second])
        #expect(try store.load() == [first, second])
        try store.save([second])
        #expect(try store.load() == [second])
        try store.save([])
        #expect(try store.load().isEmpty)
    }

    @Test func inMemoryStoreCanStartFilled() throws {
        #expect(try InMemoryManagedServerStore([first]).load() == [first])
    }

    @Test func descriptionHidesToken() {
        #expect(!first.description.contains("secret1"))
        #expect(!first.debugDescription.contains("secret1"))
        #expect(!"\(first)".contains("secret1"))
        #expect(!String(reflecting: first).contains("secret1"))
        #expect(first.description.contains("<redacted>"))
    }

    @Test func codableRoundTrip() throws {
        let data = try JSONEncoder().encode([first, second])
        #expect(try JSONDecoder().decode([ManagedServer].self, from: data) == [first, second])
    }

    @Test func decodesWithoutOptionalKeys() throws {
        let json = #"[{"id":"a","name":"Old","url":"https://old.example.com","token":"wc_pat_x"}]"#
        let decoded = try JSONDecoder().decode([ManagedServer].self, from: Data(json.utf8))
        #expect(decoded.first?.linked == false)
        #expect(decoded.first?.cloudServerID == nil)
        #expect(decoded.first?.id == "a")
    }

    @Test func defaultsToFreshIDAndUnlinked() {
        let one = ManagedServer(name: "A", url: URL(string: "https://a.example.com")!, token: "wc_pat_a")
        let two = ManagedServer(name: "A", url: URL(string: "https://a.example.com")!, token: "wc_pat_a")
        #expect(one.id != two.id)
        #expect(!one.linked)
    }
}
