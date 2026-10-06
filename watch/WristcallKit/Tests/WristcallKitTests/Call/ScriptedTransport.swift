import Foundation
import Synchronization
import WristcallKit

/// A `CallTransport` double for the cases `FakeTransport` cannot show, because it behaves
/// like a well-mannered real transport:
///
/// - It plays a scripted server: `session.start` is answered with `session.ready` plus
///   `afterStart`, and `session.end` with `afterEnd`. It keeps delivering after the client
///   hung up (nothing is dropped on its side), so only the session can filter late events.
/// - `close(code:)` returns only once the session has handled every event delivered so far
///   (its receive loop asked for the next one with nothing left), which makes "late events
///   were seen and dropped" deterministic.
/// - With `hangingBinarySends`, `send(binary:)` blocks, ignoring cancellation, until `close`
///   or `hangLimit` (a send stuck on the network).
final class ScriptedTransport: CallTransport {
    static let ready = #"{"type":"session.ready","session_id":"s1","profile":{"name":"demo","display_name":"Demo"},"audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}"#

    let events: AsyncStream<TransportEvent>
    private let inbox: Inbox
    private let afterStart: [TransportEvent]
    private let afterEnd: [TransportEvent]
    private let hangingBinarySends: Bool
    private let hangLimit: Duration
    private let log = Mutex<(texts: [String], binaries: Int, closes: [UInt16], hung: [CheckedContinuation<Void, Never>])>(([], 0, [], []))
    private let startText: String
    private let endText: String

    init(
        afterStart: [TransportEvent] = [],
        afterEnd: [TransportEvent] = [],
        hangingBinarySends: Bool = false,
        hangLimit: Duration = .seconds(6)
    ) throws {
        let inbox = Inbox()
        self.inbox = inbox
        events = AsyncStream(unfolding: { await inbox.next() })
        self.afterStart = afterStart
        self.afterEnd = afterEnd
        self.hangingBinarySends = hangingBinarySends
        self.hangLimit = hangLimit
        startText = try ClientMessage.sessionStart(profile: "demo").jsonText()
        endText = try ClientMessage.sessionEnd.jsonText()
    }

    var sentTexts: [String] { log.withLock { $0.texts } }
    var closeCodes: [UInt16] { log.withLock { $0.closes } }

    func connect() async throws {}

    func send(text: String) async throws {
        log.withLock { $0.texts.append(text) }
        if text == startText {
            inbox.push([.text(Self.ready)] + afterStart)
        } else if text == endText {
            inbox.push(afterEnd)
        }
    }

    func send(binary: Data) async throws {
        log.withLock { $0.binaries += 1 }
        guard hangingBinarySends else { return }
        // Ignores cancellation on purpose: released only by close() or the limit.
        await withCheckedContinuation { (hung: CheckedContinuation<Void, Never>) in
            log.withLock { $0.hung.append(hung) }
            Task { [hangLimit] in
                try? await Task.sleep(for: hangLimit)
                self.releaseHungSends()
            }
        }
    }

    func close(code: UInt16) async {
        log.withLock { $0.closes.append(code) }
        releaseHungSends()
        await inbox.waitUntilDrained()
    }

    /// Ends the event stream so the session's receive loop can finish (test cleanup).
    func finish() {
        inbox.finish()
    }

    private func releaseHungSends() {
        let hung = log.withLock { log in
            defer { log.hung = [] }
            return log.hung
        }
        hung.forEach { $0.resume() }
    }

    /// Server events waiting for the consumer, and who is waiting for what.
    private final class Inbox: Sendable {
        private struct State {
            var queued: [TransportEvent] = []
            var finished = false
            /// The consumer, parked in `next()` with nothing queued.
            var consumer: CheckedContinuation<TransportEvent?, Never>?
            var drainWaiters: [CheckedContinuation<Void, Never>] = []
        }

        private let state = Mutex(State())

        func push(_ events: [TransportEvent]) {
            guard !events.isEmpty else { return }
            let delivery = state.withLock { state -> (CheckedContinuation<TransportEvent?, Never>, TransportEvent)? in
                state.queued += events
                guard let consumer = state.consumer else { return nil }
                state.consumer = nil
                return (consumer, state.queued.removeFirst())
            }
            if let (consumer, event) = delivery {
                consumer.resume(returning: event)
            }
        }

        func next() async -> TransportEvent? {
            await withCheckedContinuation { consumer in
                let (immediate, drained) = state.withLock { state -> (TransportEvent??, [CheckedContinuation<Void, Never>]) in
                    if !state.queued.isEmpty { return (.some(state.queued.removeFirst()), []) }
                    if state.finished { return (.some(nil), []) }
                    // Asking for more with nothing left: everything delivered was handled.
                    state.consumer = consumer
                    defer { state.drainWaiters = [] }
                    return (nil, state.drainWaiters)
                }
                drained.forEach { $0.resume() }
                if let immediate {
                    consumer.resume(returning: immediate)
                }
            }
        }

        func waitUntilDrained() async {
            await withCheckedContinuation { waiter in
                let drained = state.withLock { state -> Bool in
                    if state.finished || (state.queued.isEmpty && state.consumer != nil) { return true }
                    state.drainWaiters.append(waiter)
                    return false
                }
                if drained {
                    waiter.resume()
                }
            }
        }

        func finish() {
            let (consumer, waiters) = state.withLock { state in
                state.finished = true
                defer {
                    state.consumer = nil
                    state.drainWaiters = []
                }
                return (state.consumer, state.drainWaiters)
            }
            consumer?.resume(returning: nil)
            waiters.forEach { $0.resume() }
        }
    }
}
