import Foundation
import WristcallKit

extension AgentDetail {
    /// A conversation agent with provider endpoints; override what a test cares about.
    static func sample(
        id: String? = nil,
        slug: String = "notes",
        displayName: String = "Notes",
        icon: String = "waveform",
        callType: String = "conversation",
        turnEnd: String = "auto",
        position: Int = 0,
        language: String = "en",
        stt: JSONValue? = .object(["provider": .string("whisper")]),
        action: JSONValue? = nil,
        tts: JSONValue? = nil,
        retentionDays: JSONValue = .null
    ) -> AgentDetail {
        let oneWay = callType != "conversation"
        return AgentDetail(
            id: id ?? "ag_\(slug)", slug: slug, displayName: displayName, icon: icon, callType: callType,
            turnEnd: turnEnd, position: position, language: language, stt: stt,
            action: action ?? .object(["provider": .string(oneWay ? "hook" : "chat")]),
            tts: tts, systemPrompt: "", fallbackMessage: "Sorry.", vad: ["silence_ms": .int(800)],
            retentionDays: retentionDays
        )
    }
}

extension ProviderList {
    /// One provider of each kind, a webhook, and custom endpoints allowed.
    static let sample = ProviderList(
        providers: [
            Provider(name: "whisper", kind: "stt"),
            Provider(name: "chat", kind: "action"),
            Provider(name: "piper", kind: "tts"),
            Provider(name: "hook", kind: "webhook"),
        ],
        customEndpoints: true
    )

    static let noCustom = ProviderList(providers: ProviderList.sample.providers, customEndpoints: false)
}
