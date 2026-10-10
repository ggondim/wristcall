import Foundation
import Observation
import WristcallKit

/// State of the create/edit agent form, and the rules the server applies to it (`agents.py`), so most
/// refusals show up before anything is sent.
///
/// `fields()` is the body of `POST /v1/agents` (everything the user filled in) or of
/// `PATCH /v1/agents/{ref}` (only what changed).
@MainActor
@Observable
final class AgentFormModel {
    enum EndpointChoice: Hashable {
        /// A provider the operator offers, by name: `{"provider": name}`.
        case provider(String)
        /// The user's own service: `{"type": ..., ...}`. Only when the server allows custom endpoints.
        case custom
        /// Nothing chosen: on creation the server uses its only provider of that kind (or refuses).
        case none
    }

    /// Days the agent's calls stay in the history.
    enum Retention: Hashable {
        case serverDefault
        case days(Int)
        case forever
    }

    /// The three steps of a call, in the agent fields they fill.
    enum Stage: CaseIterable, Hashable {
        case stt, action, tts

        var field: String {
            switch self {
            case .stt: "stt"
            case .action: "action"
            case .tts: "tts"
            }
        }
    }

    static let defaultSilenceMs = 800
    static let callTypes = ["conversation", "one-shot", "monologue"]

    // MARK: Fields

    var slug = ""
    var displayName = ""
    var icon = "waveform"
    var callType = "conversation"
    var turnEnd = "auto"
    var language = "en"
    var systemPrompt = ""
    var fallbackMessage = ""

    var stt: EndpointChoice = .none
    var action: EndpointChoice = .none
    var tts: EndpointChoice = .none

    /// One-way agents' own webhook: `{"type":"webhook","url":…}` plus at most one header.
    var webhookURL = ""
    var webhookHeaderName = ""
    var webhookHeaderValue = ""
    /// A conversation's own chat endpoint: `{"type":"openai_chat",…}`.
    var chatBaseURL = ""
    var chatModel = ""
    var chatAPIKey = ""
    /// Own speech to text (`openai_stt`) and voice (`openai_tts`) services.
    var sttBaseURL = ""
    var sttModel = ""
    var sttAPIKey = ""
    var ttsBaseURL = ""
    var ttsModel = ""
    var ttsAPIKey = ""

    /// `vad.silence_ms`, 100...10000.
    var silenceMs = AgentFormModel.defaultSilenceMs
    var retention: Retention = .serverDefault

    let editing: AgentDetail?
    let providers: ProviderList

    /// Endpoints as the form builds them right after loading; what `fields()` compares with.
    @ObservationIgnored private var initialEndpoints: [Stage: JSONValue] = [:]
    /// The one header the form shows when the stored webhook has several.
    @ObservationIgnored private var shownHeaderName = ""

    init(editing agent: AgentDetail?, providers: ProviderList) {
        self.editing = agent
        self.providers = providers
        guard let agent else { return }
        slug = agent.slug
        displayName = agent.displayName
        icon = agent.icon
        callType = agent.callType
        turnEnd = agent.turnEnd
        language = agent.language
        systemPrompt = agent.systemPrompt
        fallbackMessage = agent.fallbackMessage
        stt = Self.choice(from: agent.stt)
        action = Self.choice(from: agent.action)
        tts = Self.choice(from: agent.tts)
        silenceMs = Self.silence(of: agent)
        retention = Self.retention(of: agent)
        loadCustom(agent.stt, .stt)
        loadCustom(agent.action, .action)
        loadCustom(agent.tts, .tts)
        for stage in Stage.allCases {
            if let built = endpoint(stage) { initialEndpoints[stage] = built }
        }
    }

    var isEditing: Bool { editing != nil }
    var isOneWay: Bool { callType != "conversation" }
    var allowsCustom: Bool { providers.customEndpoints }

    // MARK: Pickers

    /// The kind of provider (in `GET /v1/providers`) that fills a stage for the current call type.
    private func kind(of stage: Stage) -> String {
        switch stage {
        case .stt: "stt"
        case .action: isOneWay ? "webhook" : "action"
        case .tts: "tts"
        }
    }

    private func providerNames(for stage: Stage) -> [String] {
        providers.providers.filter { $0.kind == kind(of: stage) }.map(\.name)
    }

    func choice(for stage: Stage) -> EndpointChoice {
        switch stage {
        case .stt: stt
        case .action: action
        case .tts: tts
        }
    }

    /// What the picker of a stage offers. "Custom" only when the server allows it (or already holds one).
    func choices(for stage: Stage) -> [EndpointChoice] {
        let current = choice(for: stage)
        var list: [EndpointChoice] = []
        if !isEditing || current == .none { list.append(.none) }
        let names = providerNames(for: stage)
        list += names.map { .provider($0) }
        if case .provider(let name) = current, !names.contains(name) { list.append(current) }
        if allowsCustom || current == .custom { list.append(.custom) }
        return list
    }

    /// Changes the call type and drops a provider choice that does not fit the new one.
    func selectCallType(_ type: String) {
        callType = type
        if case .provider(let name) = action, !providerNames(for: .action).contains(name) { action = .none }
    }

    /// `my-notes` out of `My Notes!`, for the slug field until the user types their own.
    static func suggestSlug(from name: String) -> String {
        var out = ""
        for scalar in name.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: nil).lowercased().unicodeScalars {
            let isAllowed = ("a"..."z").contains(scalar) || ("0"..."9").contains(scalar)
            if isAllowed {
                out.unicodeScalars.append(scalar)
            } else if !out.hasSuffix("-") && !out.isEmpty {
                out.append("-")
            }
        }
        out = String(out.prefix(32))
        while out.hasSuffix("-") { out.removeLast() }
        return out
    }

    // MARK: Reading a stored agent

    private static func choice(from value: JSONValue?) -> EndpointChoice {
        guard let value, value != .null else { return .none }
        if let name = value["provider"]?.stringValue { return .provider(name) }
        return value["type"] != nil ? .custom : .none
    }

    private static func silence(of agent: AgentDetail) -> Int {
        if let value = agent.vad["silence_ms"]?.doubleValue { return Int(value) }
        return defaultSilenceMs
    }

    private static func retention(of agent: AgentDetail) -> Retention {
        switch agent.retentionDays {
        case .int(let days): .days(days)
        case .string("forever"): .forever
        default: .serverDefault
        }
    }

    private func loadCustom(_ value: JSONValue?, _ stage: Stage) {
        guard let value, value["provider"] == nil, let type = value["type"]?.stringValue else { return }
        let url = value["base_url"]?.stringValue ?? ""
        let model = value["model"]?.stringValue ?? ""
        let key = value["api_key"]?.stringValue ?? ""
        switch (stage, type) {
        case (.action, "webhook"):
            webhookURL = value["url"]?.stringValue ?? ""
            if case .object(let headers)? = value["headers"], let first = headers.keys.sorted().first {
                shownHeaderName = first
                webhookHeaderName = first
                webhookHeaderValue = headers[first]?.stringValue ?? ""
            }
        case (.action, "openai_chat"):
            (chatBaseURL, chatModel, chatAPIKey) = (url, model, key)
        case (.stt, "openai_stt"):
            (sttBaseURL, sttModel, sttAPIKey) = (url, model, key)
        case (.tts, "openai_tts"):
            (ttsBaseURL, ttsModel, ttsAPIKey) = (url, model, key)
        default:
            break
        }
    }

    // MARK: Building endpoints

    private func storedEndpoint(_ stage: Stage) -> JSONValue? {
        switch stage {
        case .stt: editing?.stt
        case .action: editing?.action
        case .tts: editing?.tts
        }
    }

    /// The type a custom endpoint of this stage has for the current call type.
    private func customType(_ stage: Stage) -> String {
        switch stage {
        case .stt: "openai_stt"
        case .action: isOneWay ? "webhook" : "openai_chat"
        case .tts: "openai_tts"
        }
    }

    /// The stored endpoint, when it is a custom one of the type the stage has now (its other options and
    /// its masked secrets are kept).
    private func storedCustom(_ stage: Stage) -> [String: JSONValue]? {
        guard case .object(let object)? = storedEndpoint(stage), object["provider"] == nil,
              object["type"]?.stringValue == customType(stage) else { return nil }
        return object
    }

    private func endpoint(_ stage: Stage) -> JSONValue? {
        switch choice(for: stage) {
        case .none: nil
        case .provider(let name): .object(["provider": .string(name)])
        case .custom: customEndpoint(stage)
        }
    }

    private func customEndpoint(_ stage: Stage) -> JSONValue {
        var object = storedCustom(stage) ?? ["type": .string(customType(stage))]
        func clean(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
        if stage == .action && isOneWay {
            object["url"] = .string(clean(webhookURL))
            var headers: [String: JSONValue] = [:]
            if case .object(let stored)? = object["headers"] { headers = stored }
            headers[shownHeaderName] = nil
            let name = clean(webhookHeaderName)
            if !name.isEmpty { headers[name] = .string(webhookHeaderValue) }
            object["headers"] = headers.isEmpty ? nil : .object(headers)
        } else {
            let (url, model, key) = openAIFields(stage)
            object["base_url"] = .string(clean(url))
            object["model"] = .string(clean(model))
            object["api_key"] = key.isEmpty ? nil : .string(key)
        }
        return .object(object)
    }

    private func openAIFields(_ stage: Stage) -> (String, String, String) {
        switch stage {
        case .stt: (sttBaseURL, sttModel, sttAPIKey)
        case .action: (chatBaseURL, chatModel, chatAPIKey)
        case .tts: (ttsBaseURL, ttsModel, ttsAPIKey)
        }
    }

    /// Stages the current call type uses (a one-way agent has no voice).
    private var activeStages: [Stage] { isOneWay ? [.stt, .action] : Stage.allCases }

    /// `true` on an edit when the stage still holds what the server stored and the call type change
    /// (webhook to responder and back) does not ask for a new one.
    private func isUntouched(_ stage: Stage, _ built: JSONValue?) -> Bool {
        guard let editing, built == initialEndpoints[stage] else { return false }
        return !(stage == .action && isOneWay != editing.isOneWay)
    }

    // MARK: Validation

    private static let reservedHeaders: Set<String> = [
        "content-type", "content-length", "host", "user-agent", "idempotency-key",
        "transfer-encoding", "connection", "expect",
    ]

    private static func isHTTPURL(_ text: String, httpsOnly: Bool) -> Bool {
        guard let parts = URLComponents(string: text), let scheme = parts.scheme?.lowercased(),
              scheme == "https" || (!httpsOnly && scheme == "http"),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.query == nil, parts.fragment == nil else { return false }
        return true
    }

    /// Why the form cannot be sent yet, or `nil`.
    var validationMessage: String? {
        let name = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !isEditing, slug.wholeMatch(of: /[a-z0-9][a-z0-9-]{0,31}/) == nil {
            return "Slug: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit."
        }
        if name.isEmpty || name.count > 64 { return "Enter a name of up to 64 characters." }
        if icon.wholeMatch(of: /[a-z0-9]+(\.[a-z0-9]+)*/) == nil { return "Pick an icon." }
        if !Self.callTypes.contains(callType) { return "Pick a call type." }
        for stage in activeStages {
            if let message = validate(stage) { return message }
        }
        let languageLength = language.trimmingCharacters(in: .whitespacesAndNewlines).count
        if languageLength < 2 || languageLength > 16 { return "Language: use 2 to 16 characters, for example en or pt-BR." }
        if systemPrompt.count > 20_000 { return "The prompt can have up to 20000 characters." }
        if fallbackMessage.count > 500 || (isEditing && fallbackMessage.isEmpty) {
            return "The failure message needs 1 to 500 characters."
        }
        if !(100...10_000).contains(silenceMs) { return "Silence must be between 100 and 10000 ms." }
        if case .days(let days) = retention, !(1...36_500).contains(days) {
            return "Keep history for 1 to 36500 days."
        }
        return nil
    }

    private func label(_ stage: Stage) -> String {
        switch stage {
        case .stt: "Speech to text"
        case .action: isOneWay ? "Webhook" : "Responder"
        case .tts: "Voice"
        }
    }

    private func validate(_ stage: Stage) -> String? {
        let selected = choice(for: stage)
        if selected == .none { return isEditing ? "Choose \(label(stage))." : nil }
        let built = endpoint(stage)
        // Untouched on an edit: the server already holds it, and it is not sent.
        if isUntouched(stage, built) { return nil }
        if case .provider(let name) = selected {
            return providerNames(for: stage).contains(name) ? nil : "\(label(stage)): this server has no \"\(name)\" for this step."
        }
        guard allowsCustom else { return "\(label(stage)): this server doesn't accept custom endpoints." }
        let sameType = storedCustom(stage) != nil
        func masked(_ secret: String) -> Bool { secret == "***" && !sameType }
        let reenter = "\(label(stage)): enter the secret again, a stored one only carries over to the same kind of endpoint."
        if stage == .action && isOneWay {
            let url = webhookURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard Self.isHTTPURL(url, httpsOnly: true) else {
                return "Webhook: enter an https:// address without credentials, query or fragment."
            }
            let header = webhookHeaderName.trimmingCharacters(in: .whitespacesAndNewlines)
            if header.isEmpty != webhookHeaderValue.isEmpty { return "Webhook: a header needs a name and a value." }
            if !header.isEmpty {
                if header.wholeMatch(of: /[A-Za-z0-9!#$%&'*+.^_`|~-]+/) == nil || Self.reservedHeaders.contains(header.lowercased()) {
                    return "Webhook: that header name is invalid or set by the server."
                }
                let value = webhookHeaderValue
                if !value.allSatisfy({ $0.isASCII && !$0.isNewline && $0 >= " " && $0 != "\u{7F}" }) || value.count > 4096 {
                    return "Webhook: the header value must be printable ASCII on one line."
                }
                if masked(value) { return reenter }
            }
            return nil
        }
        let (url, model, key) = openAIFields(stage)
        guard Self.isHTTPURL(url.trimmingCharacters(in: .whitespacesAndNewlines), httpsOnly: false) else {
            return "\(label(stage)): enter the base URL (http:// or https://, no credentials or query)."
        }
        if model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "\(label(stage)): enter the model name." }
        return masked(key) ? reenter : nil
    }

    // MARK: Body

    /// Creation: everything the user filled in. Edit: only what differs from the stored agent.
    func fields() -> AgentFields {
        var out = AgentFields()
        let name = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        let lang = language.trimmingCharacters(in: .whitespacesAndNewlines)
        if let agent = editing {
            if name != agent.displayName { out["display_name"] = .string(name) }
            if icon != agent.icon { out["icon"] = .string(icon) }
            if callType != agent.callType { out["call_type"] = .string(callType) }
            if turnEnd != agent.turnEnd { out["turn_end"] = .string(turnEnd) }
            if lang != agent.language { out["language"] = .string(lang) }
            if systemPrompt != agent.systemPrompt { out["system_prompt"] = .string(systemPrompt) }
            if fallbackMessage != agent.fallbackMessage { out["fallback_message"] = .string(fallbackMessage) }
            for stage in activeStages {
                if let built = endpoint(stage), !isUntouched(stage, built) { out[stage.field] = built }
            }
            // A one-way agent has no voice; a stored one is dropped (the server would keep it otherwise).
            if isOneWay, initialEndpoints[.tts] != nil { out["tts"] = .null }
            if silenceMs != Self.silence(of: agent) { out["vad"] = .object(["silence_ms": .int(silenceMs)]) }
            if retention != Self.retention(of: agent) { out["retention_days"] = Self.encode(retention) }
            return out
        }
        out["slug"] = .string(slug)
        out["display_name"] = .string(name)
        out["icon"] = .string(icon)
        out["call_type"] = .string(callType)
        out["turn_end"] = .string(turnEnd)
        if !lang.isEmpty { out["language"] = .string(lang) }
        if !systemPrompt.isEmpty { out["system_prompt"] = .string(systemPrompt) }
        if !fallbackMessage.isEmpty { out["fallback_message"] = .string(fallbackMessage) }
        for stage in activeStages {
            if let built = endpoint(stage) { out[stage.field] = built }
        }
        if silenceMs != Self.defaultSilenceMs { out["vad"] = .object(["silence_ms": .int(silenceMs)]) }
        if retention != .serverDefault { out["retention_days"] = Self.encode(retention) }
        return out
    }

    private static func encode(_ retention: Retention) -> JSONValue {
        switch retention {
        case .serverDefault: .null
        case .days(let days): .int(days)
        case .forever: .string("forever")
        }
    }
}
