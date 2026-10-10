import Foundation
import Testing
@testable import WristcallKit

struct PendingResultStoreTests {
    let defaults = UserDefaults(suiteName: "PendingResultStoreTests.\(UUID().uuidString)")!
    let clock = Clock()

    final class Clock: @unchecked Sendable {
        var date = Date(timeIntervalSince1970: 1_800_000_000)
    }

    func makeStore() -> PendingResultStore {
        let clock = clock
        return PendingResultStore(defaults: defaults, now: { clock.date })
    }

    func result(_ id: String, age: TimeInterval = 0, agent: String? = "ag_2") -> PendingResult {
        PendingResult(callID: id, serverID: "srv-1", agentID: agent, startedAt: clock.date.addingTimeInterval(-age))
    }

    @Test func startsEmpty() {
        #expect(makeStore().latest() == nil)
    }

    @Test func addThenLatestReturnsTheNewest() {
        let store = makeStore()
        store.add(result("c_1"))
        store.add(result("c_2"))

        #expect(store.latest() == result("c_2"))
    }

    @Test func removeDropsOnlyThatCall() {
        let store = makeStore()
        store.add(result("c_1"))
        store.add(result("c_2"))

        store.remove(callID: "c_2")
        #expect(store.latest() == result("c_1"))

        store.remove(callID: "c_1")
        store.remove(callID: "unknown")
        #expect(store.latest() == nil)
    }

    @Test func addingTheSameCallAgainDoesNotDuplicateIt() {
        let store = makeStore()
        store.add(result("c_1"))
        store.add(result("c_2"))
        store.add(result("c_1"))

        #expect(store.latest() == result("c_1"))
        store.remove(callID: "c_1")
        #expect(store.latest() == result("c_2"))
    }

    @Test func keepsAtMostTenDroppingTheOldest() {
        let store = makeStore()
        for index in 1...12 { store.add(result("c_\(index)")) }

        for index in stride(from: 12, through: 3, by: -1) {
            #expect(store.latest()?.callID == "c_\(index)")
            store.remove(callID: "c_\(index)")
        }
        #expect(store.latest() == nil)
    }

    @Test func entriesOlderThanADayAreDropped() {
        let store = makeStore()
        store.add(result("c_old", age: 86_401))
        store.add(result("c_new", age: 3_600))

        #expect(store.latest()?.callID == "c_new")
        store.remove(callID: "c_new")
        // The old one went away together with the first `latest()`.
        #expect(store.latest() == nil)
    }

    @Test func onlyOldEntriesMeanNothingPending() {
        let store = makeStore()
        store.add(result("c_old", age: 90_000))

        #expect(store.latest() == nil)
        #expect(store.latest(maxAge: 1_000_000) == nil)
    }

    @Test func maxAgeIsConfigurable() {
        let store = makeStore()
        store.add(result("c_1", age: 120))

        #expect(store.latest(maxAge: 60) == nil)
    }

    @Test func survivesANewStoreOverTheSameDefaults() {
        makeStore().add(result("c_1", agent: nil))

        #expect(makeStore().latest() == result("c_1", agent: nil))
    }

    @Test func garbageInTheDefaultsIsNoEntries() {
        defaults.set(Data("not json".utf8), forKey: "wristcall.pendingResults")
        let store = makeStore()

        #expect(store.latest() == nil)
        store.add(result("c_1"))
        #expect(store.latest() == result("c_1"))
    }

    @Test func storesIdsAndDatesOnly() throws {
        let store = makeStore()
        store.add(result("c_1"))

        let data = try #require(defaults.data(forKey: "wristcall.pendingResults"))
        let keys = try #require(JSONSerialization.jsonObject(with: data) as? [[String: Any]]).flatMap(\.keys)
        #expect(Set(keys) == ["callID", "serverID", "agentID", "startedAt"])
    }
}
