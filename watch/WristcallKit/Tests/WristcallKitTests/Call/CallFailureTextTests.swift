import Testing
import WristcallKit

struct CallFailureTextTests {
    static func error(_ code: String, reason: String? = nil) -> ServerError {
        ServerError(code: ServerErrorCode(wireValue: code), message: "the server's own text", fatal: true, reason: reason)
    }

    @Test func failureTextsCoverNewCodes() {
        let table: [(String, String?, String)] = [
            ("plan_required", nil, "This account has no Wristcall Cloud plan."),
            ("quota_exceeded", nil, "You've used this month's call minutes."),
            ("busy", "too_many_calls", "Another call is still open on this account."),
            ("busy", nil, "Busy, try again in a minute."),
            ("agent_unavailable", "cloud_unreachable", "Wristcall Cloud is unavailable; try again."),
            ("agent_unavailable", "needs_voice_url", "This agent needs your own transcription and voice addresses."),
            ("agent_unavailable", "url_not_public", "This agent uses an address the server can't call."),
            ("agent_unavailable", "url_unresolved", "Can't look up this agent's address right now; try again."),
            ("no_node_available", nil, "Wristcall Cloud is temporarily unavailable. Try again in a minute."),
            ("route_unreachable", nil, "Can't reach Wristcall Cloud."),
            ("signed_out", nil, "Open wristcall on your iPhone to sign in again."),
        ]
        for (code, reason, text) in table {
            #expect(CallFailureText.text(for: Self.error(code, reason: reason)) == text, "\(code) \(reason ?? "-")")
        }

        // Everything else keeps the app's own generic text.
        #expect(CallFailureText.text(for: Self.error("internal")) == nil)
        #expect(CallFailureText.text(for: Self.error("agent_unavailable")) == nil)
        #expect(CallFailureText.text(for: Self.error("agent_unavailable", reason: "something_new")) == nil)
        #expect(CallFailureText.text(for: Self.error("route_failed")) == nil)
        #expect(CallFailureText.text(for: Self.error("stt_failed")) == nil)
    }
}
