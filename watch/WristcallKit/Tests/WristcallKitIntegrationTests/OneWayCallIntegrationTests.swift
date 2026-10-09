#if os(macOS)
import Foundation
import Synchronization
import Testing
import WristcallKit

/// One-shot calls against the local test server: record, hang up, then read the result with
/// `GET /v1/calls/{id}` until it is final. Each test adds its own agent with the CLI (the test
/// config has only conversation profiles), pointing its webhook at a `WebhookReceiver`, and
/// removes it at the end. The data dir outlives the run, so a leftover from a killed run is removed first.
@Suite(.enabled(if: TestServer.isConfigured, "set WRISTCALL_TEST_SERVER to run integration tests"))
struct OneWayCallIntegrationTests {
    /// Faster than the app's 1.5 s: the fake providers answer at once.
    let poller = CallStatusPoller(interval: .milliseconds(250), timeout: .seconds(40))

    @Test(.timeLimit(.minutes(1)))
    func oneShotIsDeliveredToTheWebhook() async throws {
        let webhook = try WebhookReceiver()
        defer { webhook.stop() }
        let agent = try OneShotAgent(slug: "it-one-shot-delivered", webhook: try await webhook.start())
        defer { agent.remove() }

        let call = try await record(agent: agent.slug, audio: WAVFixture.pcm16(named: "speech_pt_16k.wav"))
        let outcome = try await result(of: call)

        guard case .final(let status) = outcome else {
            Issue.record("expected a final status, got \(outcome)")
            return
        }
        #expect(status.id == call)
        #expect(status.callType == .oneShot)
        #expect(status.state == .delivered)
        #expect(status.failure == nil)
        #expect(status.text == "hello")
        #expect(status.attempts == 1)
        #expect(status.lastHTTPStatus == 204)

        let delivery = try #require(webhook.requests.first)
        #expect(webhook.requests.count == 1)
        #expect(delivery.method == "POST")
        #expect(delivery.path == "/hook")
        #expect(delivery.headers["idempotency-key"] == call)
        let body = try delivery.json()
        #expect(body["event"] as? String == "call.completed")
        #expect(body["call_id"] as? String == call)
        #expect(body["call_type"] as? String == "one-shot")
        #expect(body["text"] as? String == "hello")
        #expect((body["agent"] as? [String: Any])?["slug"] as? String == agent.slug)
    }

    @Test(.timeLimit(.minutes(1)))
    func oneShotWithOnlySilenceIsEmpty() async throws {
        let webhook = try WebhookReceiver()
        defer { webhook.stop() }
        let agent = try OneShotAgent(slug: "it-one-shot-empty", webhook: try await webhook.start())
        defer { agent.remove() }

        let call = try await record(agent: agent.slug, audio: Data(count: ProtocolConstants.frameBytes * 50))
        let outcome = try await result(of: call)

        guard case .final(let status) = outcome else {
            Issue.record("expected a final status, got \(outcome)")
            return
        }
        #expect(status.state == .empty)
        #expect(status.text == nil)
        #expect(webhook.requests.isEmpty)
    }

    // MARK: - Helpers

    /// Calls `agent` with the shared device, streams `audio` paced like the microphone, hangs up
    /// and returns the call id from `session.ready`.
    private func record(agent: String, audio: Data) async throws -> String {
        let device = try await TestDevices.shared()
        let transport = try NWWebSocketTransport(server: try TestServer.requireBaseURL(), token: device.token)
        let session = CallSession(transport: transport)

        let ready = try await session.start(agent: agent)
        #expect(ready.agent?.slug == agent)
        #expect(ready.agent?.callType == .oneShot)
        let callID = try #require(ready.callID)

        for frame in WAVFixture.frames(of: audio) {
            session.sendAudio(frame)
            try await Task.sleep(for: .milliseconds(20))
        }
        await session.end()

        // A one-way call gets nothing back while it records: no turns, no transcript, no voice.
        var events: [CallEvent] = []
        for await event in session.events {
            events.append(event)
        }
        #expect(events == [.ended(.normal)])
        return callID
    }

    /// Polls `GET /v1/calls/{id}` with the shared device's token, as the app does after hang-up.
    private func result(of callID: String) async throws -> CallStatusPoller.Outcome {
        let device = try await TestDevices.shared()
        let server = try TestServer.requireBaseURL()
        let client = PairingClient()
        let seen = Mutex<[CallState]>([])
        let outcome = await poller.run(
            fetch: { try await client.callStatus(server: server, token: device.token, callID: callID) },
            onUpdate: { status in seen.withLock { $0.append(status.state) } }
        )
        #expect(seen.withLock { $0 }.allSatisfy { $0 != .recording }, "the call was still open after hang-up")
        return outcome
    }
}

/// A one-shot agent of the server's single user, added with `wristcall agents add`. The suites run in
/// parallel, so it shows in `/v1/me` for a few seconds: `PairingIntegrationTests` leaves it out.
/// Its transcript comes from `demo-stt` (`fake_stt`: always "hello") and goes to `webhook`.
struct OneShotAgent {
    /// Every test agent's slug starts with it, so other tests can tell them from the config's agents.
    static let slugPrefix = "it-"

    let slug: String

    static func isTestAgent(_ slug: String) -> Bool {
        slug.hasPrefix(slugPrefix)
    }

    init(slug: String, webhook: URL) throws {
        precondition(Self.isTestAgent(slug), "test agents' slugs start with \(Self.slugPrefix)")
        self.slug = slug
        // Left over by a run that was killed before its cleanup.
        let listed = try TestServer.runCLI(["agents", "list"]).split(whereSeparator: \.isWhitespace)
        if listed.contains(Substring(slug)) {
            remove()
        }
        let action = #"{"type":"webhook","url":"\#(webhook.absoluteString)"}"#
        _ = try TestServer.runCLI([
            "agents", "add", slug,
            "--name", "One-shot test",
            "--call-type", "one-shot",
            "--stt", "demo-stt",
            "--action", action,
        ])
    }

    /// `wristcall agents rm <slug> --yes`.
    func remove() {
        _ = try? TestServer.runCLI(["agents", "rm", slug, "--yes"])
    }
}
#endif
