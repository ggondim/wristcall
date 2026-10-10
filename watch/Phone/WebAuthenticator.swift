import AuthenticationServices
import Synchronization
import UIKit

/// Opens a provider page (login, end of session) and returns the URL it redirected to. The user closing
/// the page throws `CancellationError`.
@MainActor
protocol WebAuthenticator: Sendable {
    func authenticate(url: URL, callbackScheme: String) async throws -> URL
}

enum WebAuthenticatorError: Error, Equatable {
    /// The system did not show the page (another one is up, or no window to show it on).
    case couldNotStart
}

/// The system page `LiveWebAuthenticator` drives: `ASWebAuthenticationSession`, a fake in tests.
@MainActor
protocol WebSession: AnyObject {
    func start() -> Bool
    func cancel()
}

extension ASWebAuthenticationSession: WebSession {}

/// `ASWebAuthenticationSession`, not ephemeral: the provider's cookie stays, so approving the watch's login
/// (device flow) later in the same browser needs no new sign-in. One page at a time; a new one cancels the
/// last. The session is held strongly until it ends, and cancelling the task closes the page.
///
/// Cancelling (the task, or a new page) ends the wait itself with `CancellationError`: Apple does not document
/// a completion callback for `cancel()`, and the device page (which never redirects back) must not leave its
/// caller waiting forever. A late system callback then finds the wait already over (`ResumeOnce`).
@MainActor
final class LiveWebAuthenticator: NSObject, WebAuthenticator, ASWebAuthenticationPresentationContextProviding {
    typealias Completion = @Sendable (URL?, (any Error)?) -> Void
    typealias MakeSession = @MainActor (URL, String, @escaping Completion) -> any WebSession

    private var current: (session: any WebSession, wait: ResumeOnce)?
    /// Counts the pages started, so a page that ended never clears (or cancels) a newer one.
    private var started = 0
    private let makeSession: MakeSession

    /// `makeSession` builds the system page (tests pass one that never calls back).
    init(makeSession: MakeSession? = nil) {
        self.makeSession = makeSession ?? { url, scheme, completion in
            ASWebAuthenticationSession(url: url, callback: .customScheme(scheme), completionHandler: completion)
        }
        super.init()
    }

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        cancelCurrent()
        try Task.checkCancellation()
        started += 1
        let mine = started
        defer { if started == mine { current = nil } }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (checked: CheckedContinuation<URL, any Error>) in
                let continuation = ResumeOnce(checked)
                let session = makeSession(url, callbackScheme) { callback, error in
                    // Never log `callback` or `error`: the callback carries the authorization code.
                    if let callback {
                        continuation.resume(returning: callback)
                    } else if let error = error as? ASWebAuthenticationSessionError, error.code == .canceledLogin {
                        continuation.resume(throwing: CancellationError())
                    } else {
                        continuation.resume(throwing: error ?? WebAuthenticatorError.couldNotStart)
                    }
                }
                if let system = session as? ASWebAuthenticationSession {
                    system.prefersEphemeralWebBrowserSession = false
                    system.presentationContextProvider = self
                }
                current = (session, continuation)
                if !session.start() {
                    current = nil
                    continuation.resume(throwing: WebAuthenticatorError.couldNotStart)
                }
            }
        } onCancel: {
            Task { @MainActor in
                if self.started == mine { self.cancelCurrent() }
            }
        }
    }

    /// Closes the page and ends its wait (whether or not the system calls back afterwards).
    private func cancelCurrent() {
        guard let current else { return }
        self.current = nil
        current.session.cancel()
        current.wait.resume(throwing: CancellationError())
    }

    nonisolated func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        MainActor.assumeIsolated {
            let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            let active = scenes.filter { $0.activationState == .foregroundActive }
            if let key = (active + scenes).lazy.flatMap(\.windows).first(where: \.isKeyWindow) {
                return key
            }
            if let window = (active + scenes).lazy.flatMap(\.windows).first {
                return window
            }
            // No window at all (should not happen while the app shows a button): a bare one in the first scene.
            return scenes.first.map { UIWindow(windowScene: $0) } ?? ASPresentationAnchor()
        }
    }
}

/// Resumes a continuation at most once (a session that failed to start may still call its handler).
private final class ResumeOnce: Sendable {
    private let continuation: Mutex<CheckedContinuation<URL, any Error>?>

    init(_ continuation: CheckedContinuation<URL, any Error>) {
        self.continuation = Mutex(continuation)
    }

    func resume(returning url: URL) {
        take()?.resume(returning: url)
    }

    func resume(throwing error: any Error) {
        take()?.resume(throwing: error)
    }

    private func take() -> CheckedContinuation<URL, any Error>? {
        continuation.withLock { value in
            defer { value = nil }
            return value
        }
    }
}
