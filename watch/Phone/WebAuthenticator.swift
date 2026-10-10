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

/// `ASWebAuthenticationSession`, not ephemeral: the provider's cookie stays, so approving the watch's login
/// (device flow) later in the same browser needs no new sign-in. One page at a time; a new one cancels the
/// last. The session is held strongly until it ends, and cancelling the task closes the page.
@MainActor
final class LiveWebAuthenticator: NSObject, WebAuthenticator, ASWebAuthenticationPresentationContextProviding {
    private var current: ASWebAuthenticationSession?
    /// Counts the pages started, so a page that ended never clears (or cancels) a newer one.
    private var started = 0

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        cancelCurrent()
        try Task.checkCancellation()
        started += 1
        let mine = started
        defer { if started == mine { current = nil } }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (checked: CheckedContinuation<URL, any Error>) in
                let continuation = ResumeOnce(checked)
                let session = ASWebAuthenticationSession(url: url, callback: .customScheme(callbackScheme)) { callback, error in
                    // Never log `callback` or `error`: the callback carries the authorization code.
                    if let callback {
                        continuation.resume(returning: callback)
                    } else if let error = error as? ASWebAuthenticationSessionError, error.code == .canceledLogin {
                        continuation.resume(throwing: CancellationError())
                    } else {
                        continuation.resume(throwing: error ?? WebAuthenticatorError.couldNotStart)
                    }
                }
                session.prefersEphemeralWebBrowserSession = false
                session.presentationContextProvider = self
                current = session
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

    private func cancelCurrent() {
        current?.cancel()
        current = nil
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
