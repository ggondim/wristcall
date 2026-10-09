import Foundation
import Synchronization
import Testing
import WristcallKit

struct PendingCallStoreTests {
    final class Clock: Sendable {
        private let value = Mutex(Date(timeIntervalSince1970: 1_000_000))
        var now: Date { value.withLock { $0 } }
        func advance(_ seconds: TimeInterval) { value.withLock { $0 += seconds } }
    }

    let defaults = UserDefaults(suiteName: "PendingCallStoreTests.\(UUID().uuidString)")!
    let center = NotificationCenter()
    let clock = Clock()

    var store: PendingCallStore {
        let clock = clock
        return PendingCallStore(defaults: defaults, notificationCenter: center, now: { clock.now })
    }

    @Test func nothingIsPendingAtFirst() {
        #expect(!store.isPending)
        #expect(store.consume() == nil)
    }

    @Test func aRequestIsConsumedOnce() {
        store.request()
        #expect(store.isPending)
        #expect(store.isPending)  // checking does not consume
        #expect(store.consume() == PendingCall())
        #expect(store.consume() == nil)
        #expect(!store.isPending)
    }

    @Test func aStaleRequestIsDroppedAndCleared() {
        store.request()
        clock.advance(PendingCallStore.maxAge + 1)
        #expect(!store.isPending)
        #expect(store.consume() == nil)
        clock.advance(-(PendingCallStore.maxAge + 1))
        #expect(!store.isPending)  // cleared, even though it would be fresh again
    }

    @Test func aRequestAtTheLimitIsStillFresh() {
        store.request()
        clock.advance(PendingCallStore.maxAge)
        #expect(store.consume() == PendingCall())
    }

    @Test func aRequestFromTheFutureIsDropped() {
        store.request()
        clock.advance(-60)
        #expect(store.consume() == nil)
    }

    @Test func requestPostsTheNotification() {
        let received = Mutex(0)
        let token = center.addObserver(forName: PendingCallStore.didRequest, object: nil, queue: nil) { _ in
            received.withLock { $0 += 1 }
        }
        defer { center.removeObserver(token) }

        store.request()

        #expect(received.withLock { $0 } == 1)
    }

    @Test func theRequestIsVisibleThroughAnotherStoreOnTheSameDefaults() {
        store.request()
        let other = PendingCallStore(defaults: defaults, notificationCenter: center, now: { [clock] in clock.now })
        #expect(other.consume() == PendingCall())
        #expect(!store.isPending)
    }

    @Test func theRequestCarriesTheAgent() {
        store.request(agent: "srv-1/ag_one")
        #expect(store.consume() == PendingCall(agent: "srv-1/ag_one"))
        #expect(store.consume() == nil)
    }

    @Test func aRequestWithoutAgentDoesNotKeepTheOldAgent() {
        store.request(agent: "srv-1/ag_one")
        store.request()
        #expect(store.consume() == PendingCall(agent: nil))
    }

    @Test func theAgentIsKeptUnvalidated() {
        store.request(agent: "not an agent ref")
        #expect(store.consume()?.agent == "not an agent ref")
    }

    @Test func peekShowsTheFreshRequestWithoutConsumingIt() {
        #expect(store.peek() == nil)
        store.request(agent: "srv-1/ag_one")
        #expect(store.peek() == PendingCall(agent: "srv-1/ag_one"))
        #expect(store.consume() == PendingCall(agent: "srv-1/ag_one"))
        store.request()
        clock.advance(PendingCallStore.maxAge + 1)
        #expect(store.peek() == nil)
    }

    /// The system's redial knows the agent only by its name (decision W19).
    @Test func aRedialCarriesTheAgentNameOnly() {
        store.request(agent: "srv-1/ag_one")
        store.request(agentNamed: "Notes")
        #expect(store.consume() == PendingCall(agentName: "Notes"))
        store.request(agentNamed: "Notes")
        store.request(agent: "srv-1/ag_one")
        #expect(store.consume() == PendingCall(agent: "srv-1/ag_one"))
        #expect(defaults.string(forKey: PendingCallStore.agentNameDefaultsKey) == nil)
    }

    @Test func aStaleRequestWithAgentIsDroppedAndTheAgentCleared() {
        store.request(agent: "srv-1/ag_one")
        clock.advance(PendingCallStore.maxAge + 1)
        #expect(store.consume() == nil)
        #expect(defaults.string(forKey: PendingCallStore.agentDefaultsKey) == nil)
        store.request()
        #expect(store.consume() == PendingCall(agent: nil))
    }
}
