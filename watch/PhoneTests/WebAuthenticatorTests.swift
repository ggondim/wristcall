import Foundation
import Testing
@testable import WristcallPhone

/// A system web session that never calls its completion handler (what `ASWebAuthenticationSession.cancel()`
/// may do: Apple does not document a callback for it).
@MainActor
final class SilentWebSession: WebSession {
    private(set) var started = 0
    private(set) var cancelled = 0
    var startResult = true

    nonisolated init() {}

    func start() -> Bool {
        started += 1
        return startResult
    }

    func cancel() {
        cancelled += 1
    }
}

@MainActor
struct WebAuthenticatorTests {
    let page = URL(string: "https://auth.test/device?user_code=ZXSG-KCPN")!

    func authenticator(_ sessions: SessionBox) -> LiveWebAuthenticator {
        LiveWebAuthenticator { _, _, _ in
            let session = SilentWebSession()
            sessions.all.append(session)
            return session
        }
    }

    @Test func cancellingTheTaskEndsTheWaitWithoutACallback() async throws {
        let sessions = SessionBox()
        let web = authenticator(sessions)
        let task = Task { try await web.authenticate(url: page, callbackScheme: "wristcall") }
        await waitFor { sessions.all.count == 1 }
        task.cancel()
        let result = await task.result
        #expect(throws: CancellationError.self) { try result.get() }
        #expect(sessions.all.first?.cancelled == 1)
    }

    @Test func aNewPageEndsTheOldOneWithoutACallback() async throws {
        let sessions = SessionBox()
        let web = authenticator(sessions)
        let first = Task { try await web.authenticate(url: page, callbackScheme: "wristcall") }
        await waitFor { sessions.all.count == 1 }
        let second = Task { try await web.authenticate(url: page, callbackScheme: "wristcall") }
        let result = await first.result
        #expect(throws: CancellationError.self) { try result.get() }
        #expect(sessions.all.first?.cancelled == 1)
        second.cancel()
        _ = await second.result
    }

    @Test func aPageThatDoesNotStartFails() async throws {
        let web = LiveWebAuthenticator { _, _, _ in
            let session = SilentWebSession()
            session.startResult = false
            return session
        }
        await #expect(throws: WebAuthenticatorError.couldNotStart) {
            try await web.authenticate(url: page, callbackScheme: "wristcall")
        }
    }

    @Test func callbackStillWins() async throws {
        let callback = URL(string: "wristcall://auth/callback?code=c&state=s")!
        let web = LiveWebAuthenticator { _, _, completion in
            let session = SilentWebSession()
            completion(callback, nil)
            return session
        }
        #expect(try await web.authenticate(url: page, callbackScheme: "wristcall") == callback)
    }
}

@MainActor
final class SessionBox {
    var all: [SilentWebSession] = []
}

@MainActor
private func waitFor(_ condition: () -> Bool) async {
    for _ in 0..<200 where !condition() {
        try? await Task.sleep(for: .milliseconds(10))
    }
}
