import Foundation
import Observation
import WristcallKit

/// The starting points of a new agent (the chips at the top of the editor).
enum AgentTemplate: CaseIterable, Hashable {
    case voiceNotes, openAICompatible, webhook

    var title: String {
        switch self {
        case .voiceNotes: "Voice notes"
        case .openAICompatible: "OpenAI-compatible"
        case .webhook: "Webhook"
        }
    }

    /// The name a new agent gets when the user has not typed one.
    var defaultName: String {
        switch self {
        case .voiceNotes: "Voice notes"
        case .openAICompatible: "Assistant"
        case .webhook: "Webhook"
        }
    }

    var callType: String {
        switch self {
        case .voiceNotes, .webhook: "one-shot"
        case .openAICompatible: "conversation"
        }
    }

    var icon: String {
        switch self {
        case .voiceNotes: "note.text"
        case .openAICompatible: "sparkles"
        case .webhook: "bolt.horizontal"
        }
    }

    var color: AgentColor {
        switch self {
        case .voiceNotes: .yellow
        case .openAICompatible: .indigo
        case .webhook: .orange
        }
    }
}

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
    /// The accent color; sent only to servers that know it (`supportsColor`).
    var color: AgentColor?
    /// The template a new agent started from (highlights its chip).
    private(set) var template: AgentTemplate?
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
    /// `version` of the server's `/v1/health`; `nil` while unknown.
    let serverVersion: String?

    /// Endpoints as the form builds them right after loading; what `fields()` compares with.
    @ObservationIgnored private var initialEndpoints: [Stage: JSONValue] = [:]
    /// The one header the form shows when the stored webhook has several.
    @ObservationIgnored private var shownHeaderName = ""

    init(editing agent: AgentDetail?, providers: ProviderList, serverVersion: String? = nil) {
        self.editing = agent
        self.providers = providers
        self.serverVersion = serverVersion
        guard let agent else { return }
        slug = agent.slug
        displayName = agent.displayName
        icon = agent.icon
        color = agent.color
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

    /// The first server version that stores an agent `color`.
    static let colorVersion = "0.8.1"

    /// Servers before 0.8.1 refuse unknown fields, so the color is neither offered nor sent to them. An unknown
    /// version counts as old.
    var supportsColor: Bool {
        guard let serverVersion else { return false }
        return Self.version(serverVersion, isAtLeast: Self.colorVersion)
    }

    /// Numeric comparison of dotted versions (`0.10.0` is after `0.8.1`); a suffix such as `rc1` or `-dev` is
    /// ignored and a version without digits is never enough.
    static func version(_ text: String, isAtLeast minimum: String) -> Bool {
        func parts(_ text: String) -> [Int] {
            var out: [Int] = []
            for piece in text.split(separator: ".") {
                guard let number = Int(piece.prefix { $0.isNumber }) else { break }
                out.append(number)
            }
            return out
        }
        let have = parts(text), need = parts(minimum)
        guard !have.isEmpty else { return false }
        for index in 0..<max(have.count, need.count) {
            let left = index < have.count ? have[index] : 0
            let right = index < need.count ? need[index] : 0
            if left != right { return left > right }
        }
        return true
    }

    // MARK: Templates and chat URL

    /// What the URL field holds (the pasted `…/chat/completions` address is accepted as it is).
    var chatURL: String {
        get { chatBaseURL }
        set { chatBaseURL = newValue }
    }

    /// The base URL of an OpenAI-compatible service out of what the user typed: without `/chat/completions` and
    /// the trailing slash. `nil` when it is not an http(s) address.
    static func chatBaseURL(_ text: String) -> String? {
        var url = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard isHTTPURL(url, httpsOnly: false) else { return nil }
        func dropSlashes() { while url.hasSuffix("/") { url.removeLast() } }
        dropSlashes()
        let suffix = "/chat/completions"
        if url.lowercased().hasSuffix(suffix) {
            url.removeLast(suffix.count)
            dropSlashes()
        }
        return url
    }

    private var nameIsATemplateDefault: Bool {
        let name = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        return name.isEmpty || AgentTemplate.allCases.contains { $0.defaultName == name }
    }

    /// Fills type, icon, color and (when the server allows custom endpoints) the endpoint kind. A name the user
    /// typed stays; the name a previous template gave is replaced.
    func apply(_ template: AgentTemplate) {
        selectCallType(template.callType)
        self.template = template
        icon = template.icon
        color = template.color
        if nameIsATemplateDefault { displayName = template.defaultName }
        guard allowsCustom else { return }
        switch template {
        case .voiceNotes:
            // Notes only go to History: no endpoint of its own.
            if action == .custom { action = .none }
        case .openAICompatible, .webhook:
            action = .custom
        }
    }

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
        // The chip stays lit while the call stays of the same family (one-way or conversation).
        if let template, (template.callType != "conversation") != isOneWay { self.template = nil }
        if case .provider(let name) = action, !providerNames(for: .action).contains(name) { action = .none }
        // A server that only takes custom endpoints has nothing else to pick: the endpoint is the custom one.
        if allowsCustom, action == .none, providerNames(for: .action).isEmpty, template != .voiceNotes || !isOneWay {
            action = .custom
        }
    }

    /// Copies what the user typed from the form of another server (the picker rebuilds the form with that
    /// server's providers).
    func carryOver(from other: AgentFormModel) {
        displayName = other.displayName
        slug = other.slug
        selectCallType(other.callType)
        template = other.template
        icon = other.icon
        color = other.color
        if other.action == .custom, allowsCustom { action = .custom }
        webhookURL = other.webhookURL
        webhookHeaderName = other.webhookHeaderName
        webhookHeaderValue = other.webhookHeaderValue
        chatBaseURL = other.chatBaseURL
        chatModel = other.chatModel
        chatAPIKey = other.chatAPIKey
    }

    /// `true` when the endpoint section needs the picker (the server has providers for it, or there is nothing
    /// else to show); without it the custom fields stand alone.
    var endpointHasPicker: Bool {
        if !allowsCustom || (isEditing && action == .none) { return true }
        return choices(for: .action).contains { if case .provider = $0 { true } else { false } }
    }

    /// `true` when the editor shows the endpoint section.
    var showsEndpoint: Bool { endpointHasPicker || action == .custom }

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
        case .action: (Self.chatBaseURL(chatBaseURL) ?? chatBaseURL, chatModel, chatAPIKey)
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
                // A stored secret only carries over under the name it was stored with.
                if value == "***" && header != shownHeaderName { return reenter }
            }
            return nil
        }
        let (url, model, key) = openAIFields(stage)
        let isChat = stage == .action
        guard Self.isHTTPURL(url.trimmingCharacters(in: .whitespacesAndNewlines), httpsOnly: false) else {
            return isChat ? "Enter an http(s) URL."
                : "\(label(stage)): enter the base URL (http:// or https://, no credentials or query)."
        }
        if model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return isChat ? "Enter the model name." : "\(label(stage)): enter the model name."
        }
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
            if supportsColor, let color, color != agent.color { out["color"] = .string(color.rawValue) }
            if callType != agent.callType { out["call_type"] = .string(callType) }
            if turnEnd != agent.turnEnd { out["turn_end"] = .string(turnEnd) }
            if lang != agent.language { out["language"] = .string(lang) }
            if systemPrompt != agent.systemPrompt { out["system_prompt"] = .string(systemPrompt) }
            if fallbackMessage != agent.fallbackMessage { out["fallback_message"] = .string(fallbackMessage) }
            for stage in activeStages {
                if let built = endpoint(stage), !isUntouched(stage, built) { out[stage.field] = built }
            }
            // Switching to one-way drops the stored voice (the server would keep it otherwise); an agent that is
            // already one-way keeps whatever it holds.
            if isOneWay, !agent.isOneWay, initialEndpoints[.tts] != nil { out["tts"] = .null }
            if silenceMs != Self.silence(of: agent) { out["vad"] = .object(["silence_ms": .int(silenceMs)]) }
            if retention != Self.retention(of: agent) { out["retention_days"] = Self.encode(retention) }
            return out
        }
        out["slug"] = .string(slug)
        out["display_name"] = .string(name)
        out["icon"] = .string(icon)
        if supportsColor, let color { out["color"] = .string(color.rawValue) }
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
