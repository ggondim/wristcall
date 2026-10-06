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
        #expect(!store.consume())
    }

    @Test func aRequestIsConsumedOnce() {
        store.request()
        #expect(store.isPending)
        #expect(store.isPending)  // checking does not consume
        #expect(store.consume())
        #expect(!store.consume())
        #expect(!store.isPending)
    }

    @Test func aStaleRequestIsDroppedAndCleared() {
        store.request()
        clock.advance(PendingCallStore.maxAge + 1)
        #expect(!store.isPending)
        #expect(!store.consume())
        clock.advance(-(PendingCallStore.maxAge + 1))
        #expect(!store.isPending)  // cleared, even though it would be fresh again
    }

    @Test func aRequestAtTheLimitIsStillFresh() {
        store.request()
        clock.advance(PendingCallStore.maxAge)
        #expect(store.consume())
    }

    @Test func aRequestFromTheFutureIsDropped() {
        store.request()
        clock.advance(-60)
        #expect(!store.consume())
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
        #expect(other.consume())
        #expect(!store.isPending)
    }
}
