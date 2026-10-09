import Foundation
import Synchronization
import Testing
import WristcallKit

struct CallStatusPollerTests {
    /// A clock that only moves when the poller sleeps, so 3 minutes of polling take no time.
    final class FakeClock: Sendable {
        private let start = ContinuousClock.now
        private let elapsed = Mutex(Duration.zero)
        private let recorded = Mutex<[Duration]>([])

        var now: ContinuousClock.Instant { start + elapsed.withLock { $0 } }
        var sleeps: [Duration] { recorded.withLock { $0 } }

        var sleep: CallStatusPoller.Sleep {
            { [self] duration in
                try Task.checkCancellation()
                recorded.withLock { $0.append(duration) }
                elapsed.withLock { $0 += duration }
            }
        }
    }

    /// Answers the n-th fetch with `replies[n]`, repeating the last one after that, and records every update.
    final class Script: Sendable {
        private let replies: [Result<CallStatus, PairingError>]
        private let count = Mutex(0)
        private let seen = Mutex<[CallStatus]>([])

        init(_ replies: [Result<CallStatus, PairingError>]) {
            self.replies = replies
        }

        var fetches: Int { count.withLock { $0 } }
        var updates: [CallStatus] { seen.withLock { $0 } }

        func fetch() throws -> CallStatus {
            let index = count.withLock { value in
                defer { value += 1 }
                return value
            }
            return try replies[min(index, replies.count - 1)].get()
        }

        func update(_ status: CallStatus) {
            seen.withLock { $0.append(status) }
        }
    }

    let clock = FakeClock()

    func poller() -> CallStatusPoller {
        let clock = clock
        return CallStatusPoller(sleep: clock.sleep, now: { clock.now })
    }

    func run(_ script: Script) async -> CallStatusPoller.Outcome {
        await poller().run(fetch: { try script.fetch() }, onUpdate: { script.update($0) })
    }

    static func call(_ state: CallState, failure: CallFailure? = nil, text: String? = nil) -> CallStatus {
        CallStatus(id: "c_1", callType: .oneShot, state: state, failure: failure, text: text)
    }

    @Test func defaultsFollowTheProtocol() {
        let poller = CallStatusPoller()
        #expect(poller.interval == .milliseconds(1500))
        #expect(poller.timeout == .seconds(180))
    }

    @Test func pollsRightAwayThenEveryIntervalUntilFinal() async {
        let delivered = Self.call(.delivered, text: "buy milk")
        let script = Script([.success(Self.call(.recording)), .success(Self.call(.processing)), .success(delivered)])
        #expect(await run(script) == .final(delivered))
        #expect(script.fetches == 3)
        #expect(clock.sleeps == [.milliseconds(1500), .milliseconds(1500)])
        #expect(script.updates == [Self.call(.recording), Self.call(.processing), delivered])
    }

    @Test(arguments: [CallState.failed, .empty, .ended])
    func everyFinalStateStops(state: CallState) async {
        let script = Script([.success(Self.call(state))])
        #expect(await run(script) == .final(Self.call(state)))
        #expect(script.fetches == 1)
        #expect(clock.sleeps.isEmpty)
    }

    @Test func networkFailuresInTheMiddleKeepPolling() async {
        let failed = Self.call(.failed, failure: .deliveryFailed)
        let script = Script([
            .success(Self.call(.processing)),
            .failure(.network(.notConnectedToInternet)),
            .failure(.unexpectedStatus(503)),
            .failure(.malformedResponse),
            .success(failed),
        ])
        #expect(await run(script) == .final(failed))
        #expect(script.fetches == 5)
        #expect(clock.sleeps.count == 4)
        #expect(script.updates == [Self.call(.processing), failed])
    }

    @Test func anUnknownStateIsNotFinal() async {
        let script = Script([.success(Self.call(.unknown("archiving"))), .success(Self.call(.empty))])
        #expect(await run(script) == .final(Self.call(.empty)))
        #expect(script.fetches == 2)
    }

    @Test func theTimeoutReturnsTheLastStatusSeen() async {
        let processing = Self.call(.processing, text: "buy")
        let script = Script([.success(Self.call(.recording)), .success(processing), .failure(.network(.timedOut))])
        #expect(await run(script) == .timedOut(processing))
        // Right away, then every 1.5 s up to and including the 180 s mark.
        #expect(script.fetches == 121)
        #expect(clock.sleeps.count == 120)
    }

    @Test func theTimeoutWithoutAnyAnswerHasNoStatus() async {
        let script = Script([.failure(.network(.cannotConnectToHost))])
        #expect(await run(script) == .timedOut(nil))
        #expect(script.updates.isEmpty)
    }

    @Test func notFoundStopsAtOnce() async {
        let script = Script([.success(Self.call(.processing)), .failure(.notFound)])
        #expect(await run(script) == .notFound)
        #expect(script.fetches == 2)
    }

    @Test func unauthorizedStopsAtOnce() async {
        let script = Script([.failure(.unauthorized), .success(Self.call(.delivered))])
        #expect(await run(script) == .unauthorized)
        #expect(script.fetches == 1)
        #expect(clock.sleeps.isEmpty)
    }

    @Test func cancellingTheTaskStopsPolling() async {
        let (fetched, signal) = AsyncStream.makeStream(of: Void.self)
        let script = Script([.success(Self.call(.processing))])
        // A real sleep: only the cancellation can end it, so the test never waits the interval.
        let poller = CallStatusPoller(sleep: { try await Task.sleep(for: $0) })
        let task = Task {
            await poller.run(
                fetch: {
                    defer { signal.yield() }
                    return try script.fetch()
                },
                onUpdate: { script.update($0) }
            )
        }
        for await _ in fetched { break }
        task.cancel()
        #expect(await task.value == .cancelled)
        #expect(script.fetches == 1)
    }

    @Test func aCancelledFetchEndsAsCancelled() async {
        let script = Script([.success(Self.call(.processing))])
        let outcome = await poller().run(
            fetch: {
                // What URLSession throws when the task is cancelled mid-request.
                withUnsafeCurrentTask { $0?.cancel() }
                throw PairingError.network(.cancelled)
            },
            onUpdate: { script.update($0) }
        )
        #expect(outcome == .cancelled)
        #expect(clock.sleeps.isEmpty)
    }
}
