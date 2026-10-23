import Foundation

/// What a call through the Wristcall Cloud entry shows when it fails: the app's own text for the Cloud's codes (the
/// server's `message` is ignored). Only for calls of the cloud entry: a self-hosted server's `busy` or
/// `agent_unavailable` keeps the app's generic "Call failed (code)." text.
public enum CallFailureText {
    /// `nil` when the app has no text of its own for `error` (it shows its generic text).
    public static func text(for error: ServerError) -> String? {
        switch error.code {
        case .planRequired: planRequired
        case .quotaExceeded: "You've used this month's call minutes."
        // A reason this build does not know reads as the plain busy.
        case .busy: error.reason == "too_many_calls" ? "Another call is still open on this account." : busy
        case .agentUnavailable:
            switch error.reason {
            case "cloud_unreachable": cloudUnavailable
            case "needs_voice_url": "This agent needs your own transcription and voice addresses."
            case "url_not_public": "This agent uses an address the server can't call."
            case "url_unresolved": "Can't look up this agent's address right now; try again."
            default: nil
            }
        case .unknown(RouteFailure.noNodeAvailable): noNodeAvailable
        case .unknown(RouteFailure.unreachable): unreachable
        case .unknown(RouteFailure.signedOut): signedOut
        case .unknown(RouteFailure.agentNotFound): agentNotFound
        default: nil
        }
    }

    static let planRequired = "This account has no Wristcall Cloud plan."
    static let busy = "Busy, try again in a minute."
    static let cloudUnavailable = "Wristcall Cloud is unavailable; try again."
    static let noNodeAvailable = "Wristcall Cloud is temporarily unavailable. Try again in a minute."
    static let unreachable = "Can't reach Wristcall Cloud."
    static let signedOut = "Open wristcall on your iPhone to sign in again."
    /// The watch's own text for an agent that is gone (the app's "Agent not found.").
    static let agentNotFound = "Agent not found."
}

/// Local codes (`ServerErrorCode.unknown`) for a call whose route the Cloud did not give (`RoutedCallTransport`).
enum RouteFailure {
    /// `503 no_node_available`.
    static let noNodeAvailable = "no_node_available"
    /// No network, or no answer within the route timeout.
    static let unreachable = "route_unreachable"
    /// The account's session ended (`401`, revoked, `403` such as `device_revoked`, or signed out on this device).
    static let signedOut = "signed_out"
    /// `404`: the Cloud has no such agent for the account (deleted or moved since the grid was read).
    static let agentNotFound = "agent_not_found"
    /// Anything else; the message is the error's own.
    static let failed = "route_failed"
}
