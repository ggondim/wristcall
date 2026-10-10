import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct AgentFormModelTests {
    private func creating(_ providers: ProviderList = .sample) -> AgentFormModel {
        AgentFormModel(editing: nil, providers: providers)
    }

    private let webhook: JSONValue = .object(["type": .string("webhook"), "url": .string("https://h.test")])

    @Test func editOnlySendsChangedFields() {
        let model = AgentFormModel(editing: .sample(displayName: "Notes"), providers: .sample)
        #expect(model.fields().isEmpty)
        model.displayName = "Inbox"
        #expect(model.fields() == ["display_name": .string("Inbox")])
    }

    @Test func switchingToOneWayDropsTTS() {
        let model = AgentFormModel(
            editing: .sample(callType: "conversation", tts: .object(["provider": .string("piper")])),
            providers: .sample
        )
        model.callType = "one-shot"
        model.action = .custom
        model.webhookURL = "https://hooks.test/in"
        let f = model.fields()
        #expect(f["tts"] == .null)
        #expect(f["action"] == .object(["type": .string("webhook"), "url": .string("https://hooks.test/in")]))
        #expect(f["call_type"] == .string("one-shot"))
        #expect(model.validationMessage == nil)
    }

    @Test func untouchedSecretIsNotResent() {
        let action: JSONValue = .object([
            "type": .string("webhook"), "url": .string("https://h.test"),
            "headers": .object(["X-Key": .string("***")]),
        ])
        let model = AgentFormModel(editing: .sample(callType: "one-shot", action: action), providers: .sample)
        model.displayName = "Other"
        #expect(model.fields()["action"] == nil)
        #expect(model.webhookHeaderName == "X-Key")
        #expect(model.webhookHeaderValue == "***")
    }

    @Test func editedURLKeepsTheMaskedSecret() {
        let action: JSONValue = .object([
            "type": .string("webhook"), "url": .string("https://h.test"),
            "headers": .object(["X-Key": .string("***")]),
        ])
        let model = AgentFormModel(editing: .sample(callType: "one-shot", action: action), providers: .sample)
        model.webhookURL = "https://other.test/in"
        #expect(model.validationMessage == nil)
        #expect(model.fields()["action"] == .object([
            "type": .string("webhook"), "url": .string("https://other.test/in"),
            "headers": .object(["X-Key": .string("***")]),
        ]))
    }

    @Test func createSendsSlugAndDefaults() {
        let model = creating()
        model.slug = "inbox"
        model.displayName = "Inbox"
        model.stt = .provider("whisper")
        model.action = .provider("chat")
        model.tts = .provider("piper")
        #expect(model.validationMessage == nil)
        let f = model.fields()
        #expect(f["slug"] == .string("inbox"))
        #expect(f["display_name"] == .string("Inbox"))
        #expect(f["icon"] == .string("waveform"))
        #expect(f["call_type"] == .string("conversation"))
        #expect(f["turn_end"] == .string("auto"))
        #expect(f["stt"] == .object(["provider": .string("whisper")]))
        #expect(f["action"] == .object(["provider": .string("chat")]))
        #expect(f["tts"] == .object(["provider": .string("piper")]))
        #expect(f["retention_days"] == nil)
        #expect(f["vad"] == nil)
    }

    @Test func createWithoutChoiceOmitsEndpoints() {
        let model = creating()
        model.slug = "inbox"
        model.displayName = "Inbox"
        let f = model.fields()
        #expect(f["stt"] == nil && f["action"] == nil && f["tts"] == nil)
        #expect(model.validationMessage == nil)
    }

    @Test func createOneWayHasNoTTS() {
        let model = creating()
        model.slug = "hook"
        model.displayName = "Hook"
        model.callType = "monologue"
        model.tts = .provider("piper")
        model.stt = .provider("whisper")
        model.action = .custom
        model.webhookURL = "https://example.com/hook"
        let f = model.fields()
        #expect(f["tts"] == nil)
        #expect(f["action"] == .object(["type": .string("webhook"), "url": .string("https://example.com/hook")]))
        #expect(model.isOneWay)
    }

    @Test func slugValidation() {
        let model = creating()
        model.displayName = "Inbox"
        model.slug = "Bad Slug"
        #expect(model.validationMessage?.lowercased().contains("slug") == true)
        model.slug = "-bad"
        #expect(model.validationMessage != nil)
        model.slug = String(repeating: "a", count: 33)
        #expect(model.validationMessage != nil)
        model.slug = "good-1"
        #expect(model.validationMessage == nil)
    }

    @Test func slugIsSuggestedFromName() {
        #expect(AgentFormModel.suggestSlug(from: "My Notes & Ideas!") == "my-notes-ideas")
        #expect(AgentFormModel.suggestSlug(from: "  Café Ünï ") == "cafe-uni")
        #expect(AgentFormModel.suggestSlug(from: "---") == "")
        #expect(AgentFormModel.suggestSlug(from: String(repeating: "ab ", count: 30)).count <= 32)
    }

    @Test func nameValidation() {
        let model = creating()
        model.slug = "inbox"
        #expect(model.validationMessage != nil)
        model.displayName = "   "
        #expect(model.validationMessage != nil)
        model.displayName = String(repeating: "x", count: 65)
        #expect(model.validationMessage != nil)
        model.displayName = "Fine"
        #expect(model.validationMessage == nil)
        #expect(model.fields()["display_name"] == .string("Fine"))
    }

    @Test func insecureWebhookRejected() {
        let model = creating()
        model.slug = "hook"
        model.displayName = "Hook"
        model.callType = "one-shot"
        model.action = .custom
        model.webhookURL = "http://h.test"
        #expect(model.validationMessage?.contains("https") == true)
        model.webhookURL = "https://user:pw@h.test"
        #expect(model.validationMessage != nil)
        model.webhookURL = "https://h.test/in?x=1"
        #expect(model.validationMessage != nil)
        model.webhookURL = "https://h.test/in"
        #expect(model.validationMessage == nil)
    }

    @Test func webhookHeaderNeedsNameAndValue() {
        let model = creating()
        model.slug = "hook"
        model.displayName = "Hook"
        model.callType = "one-shot"
        model.action = .custom
        model.webhookURL = "https://h.test"
        model.webhookHeaderName = "Authorization"
        #expect(model.validationMessage != nil)
        model.webhookHeaderValue = "Bearer abc"
        #expect(model.validationMessage == nil)
        #expect(model.fields()["action"] == .object([
            "type": .string("webhook"), "url": .string("https://h.test"),
            "headers": .object(["Authorization": .string("Bearer abc")]),
        ]))
        model.webhookHeaderName = "Host"
        #expect(model.validationMessage != nil)
    }

    @Test func retentionEncodesInt() {
        let model = AgentFormModel(editing: .sample(), providers: .sample)
        model.retention = .days(30)
        #expect(model.fields() == ["retention_days": .int(30)])
        model.retention = .days(0)
        #expect(model.validationMessage != nil)
        model.retention = .days(36_501)
        #expect(model.validationMessage != nil)
    }

    @Test func retentionForever() {
        let model = AgentFormModel(editing: .sample(), providers: .sample)
        model.retention = .forever
        #expect(model.fields() == ["retention_days": .string("forever")])
    }

    @Test func retentionBackToServerDefaultSendsNull() {
        let model = AgentFormModel(editing: .sample(retentionDays: .int(7)), providers: .sample)
        #expect(model.retention == .days(7))
        #expect(model.fields().isEmpty)
        model.retention = .serverDefault
        #expect(model.fields() == ["retention_days": .null])
        let forever = AgentFormModel(editing: .sample(retentionDays: .string("forever")), providers: .sample)
        #expect(forever.retention == .forever)
    }

    @Test func customHiddenWhenServerForbids() {
        let model = creating(.noCustom)
        #expect(model.allowsCustom == false)
        for stage in AgentFormModel.Stage.allCases {
            #expect(!model.choices(for: stage).contains(.custom))
        }
        #expect(creating().choices(for: .action).contains(.custom))
        model.slug = "x"
        model.displayName = "X"
        model.action = .custom
        #expect(model.validationMessage != nil)
    }

    @Test func choicesFollowKindAndCallType() {
        let model = creating()
        #expect(model.choices(for: .stt) == [.none, .provider("whisper"), .custom])
        #expect(model.choices(for: .action) == [.none, .provider("chat"), .custom])
        #expect(model.choices(for: .tts) == [.none, .provider("piper"), .custom])
        model.callType = "one-shot"
        #expect(model.choices(for: .action) == [.none, .provider("hook"), .custom])
    }

    @Test func oneWayToConversationNeedsResponderAndTTS() {
        let action: JSONValue = .object(["type": .string("webhook"), "url": .string("https://h.test")])
        let model = AgentFormModel(editing: .sample(callType: "one-shot", action: action), providers: .sample)
        model.callType = "conversation"
        #expect(model.validationMessage != nil)
        model.action = .provider("chat")
        #expect(model.validationMessage != nil)  // still no TTS
        model.tts = .provider("piper")
        #expect(model.validationMessage == nil)
        let f = model.fields()
        #expect(f["call_type"] == .string("conversation"))
        #expect(f["action"] == .object(["provider": .string("chat")]))
        #expect(f["tts"] == .object(["provider": .string("piper")]))
        #expect(f["stt"] == nil)
    }

    @Test func oneWayWithWebhookProviderNeedsResponderAfterSwitch() {
        // The stored action is a webhook provider: it is not a responder, so it must be chosen again.
        let model = AgentFormModel(
            editing: .sample(callType: "one-shot", action: .object(["provider": .string("hook")])),
            providers: .sample
        )
        model.callType = "conversation"
        model.tts = .provider("piper")
        #expect(model.validationMessage != nil)
    }

    @Test func changingCustomTypeResendsEndpoint() {
        let chat: JSONValue = .object([
            "type": .string("openai_chat"), "base_url": .string("https://llm.test/v1"),
            "model": .string("m"), "api_key": .string("***"),
        ])
        let model = AgentFormModel(
            editing: .sample(action: chat, tts: .object(["provider": .string("piper")])), providers: .sample
        )
        #expect(model.chatBaseURL == "https://llm.test/v1")
        #expect(model.chatModel == "m")
        #expect(model.chatAPIKey == "***")
        #expect(model.fields().isEmpty)
        model.callType = "one-shot"
        model.webhookURL = "https://h.test/in"
        // The whole webhook goes out, and no "***" crosses to another type.
        #expect(model.fields()["action"] == .object(["type": .string("webhook"), "url": .string("https://h.test/in")]))
        // Back to a chat of the same type: nothing to resend.
        model.callType = "conversation"
        #expect(model.fields()["action"] == nil)
    }

    @Test func maskedSecretNeedsReEntryWhenTypeChanges() {
        let webhook: JSONValue = .object([
            "type": .string("webhook"), "url": .string("https://h.test"),
            "headers": .object(["X-Key": .string("***")]),
        ])
        let model = AgentFormModel(editing: .sample(callType: "one-shot", action: webhook), providers: .sample)
        model.callType = "conversation"
        model.tts = .provider("piper")
        model.action = .custom
        model.chatBaseURL = "https://llm.test/v1"
        model.chatModel = "m"
        #expect(model.validationMessage == nil)
        model.chatAPIKey = "***"
        #expect(model.validationMessage != nil)
        model.chatAPIKey = "sk-new"
        #expect(model.fields()["action"] == .object([
            "type": .string("openai_chat"), "base_url": .string("https://llm.test/v1"),
            "model": .string("m"), "api_key": .string("sk-new"),
        ]))
    }

    @Test func customChatEditKeepsOtherOptions() {
        let chat: JSONValue = .object([
            "type": .string("openai_chat"), "base_url": .string("https://llm.test/v1"), "model": .string("m"),
            "api_key": .string("***"), "extra_body": .object(["temperature": .double(0.2)]),
        ])
        let model = AgentFormModel(editing: .sample(action: chat, tts: .object(["provider": .string("piper")])), providers: .sample)
        model.chatModel = "m2"
        guard case .object(let sent)? = model.fields()["action"] else {
            Issue.record("no action")
            return
        }
        #expect(sent["model"] == .string("m2"))
        #expect(sent["api_key"] == .string("***"))
        #expect(sent["extra_body"] == .object(["temperature": .double(0.2)]))
    }

    @Test func customSTTNeedsBaseURLAndModel() {
        let model = creating()
        model.slug = "x"
        model.displayName = "X"
        model.stt = .custom
        #expect(model.validationMessage != nil)
        model.sttBaseURL = "ftp://stt.test"
        model.sttModel = "whisper-1"
        #expect(model.validationMessage != nil)
        model.sttBaseURL = "https://stt.test/v1"
        #expect(model.validationMessage == nil)
        #expect(model.fields()["stt"] == .object([
            "type": .string("openai_stt"), "base_url": .string("https://stt.test/v1"), "model": .string("whisper-1"),
        ]))
    }

    @Test func silenceIsBoundedAndSentWhenChanged() {
        let model = creating()
        model.slug = "x"
        model.displayName = "X"
        model.silenceMs = 50
        #expect(model.validationMessage != nil)
        model.silenceMs = 1200
        #expect(model.validationMessage == nil)
        #expect(model.fields()["vad"] == .object(["silence_ms": .int(1200)]))
        let editing = AgentFormModel(editing: .sample(), providers: .sample)
        #expect(editing.silenceMs == 800)
        editing.silenceMs = 900
        #expect(editing.fields() == ["vad": .object(["silence_ms": .int(900)])])
    }

    @Test func editingExistingFieldsAreLoaded() {
        let agent = AgentDetail.sample(
            slug: "daily", displayName: "Daily", icon: "brain", callType: "monologue", turnEnd: "manual",
            language: "pt-BR", action: .object(["provider": .string("hook")])
        )
        let model = AgentFormModel(editing: agent, providers: .sample)
        #expect(model.isEditing)
        #expect(model.slug == "daily")
        #expect(model.icon == "brain")
        #expect(model.callType == "monologue")
        #expect(model.turnEnd == "manual")
        #expect(model.language == "pt-BR")
        #expect(model.stt == .provider("whisper"))
        #expect(model.action == .provider("hook"))
        #expect(model.tts == .none)
        #expect(model.isOneWay)
        #expect(model.validationMessage == nil)
    }

    @Test func slugIsNeverSentOnEdit() {
        let model = AgentFormModel(editing: .sample(), providers: .sample)
        model.slug = "other"
        #expect(model.fields()["slug"] == nil)
    }

    @Test func iconNamesAreValidSymbols() {
        for symbol in IconPicker.symbols {
            #expect(symbol.wholeMatch(of: /[a-z0-9]+(\.[a-z0-9]+)*/) != nil, "\(symbol)")
        }
        #expect(IconPicker.symbols.count == 20)
        let model = creating()
        model.slug = "x"
        model.displayName = "X"
        model.icon = "Not A Symbol"
        #expect(model.validationMessage != nil)
    }

    @Test func languageAndTextBounds() {
        let model = AgentFormModel(editing: .sample(), providers: .sample)
        model.language = "x"
        #expect(model.validationMessage != nil)
        model.language = "pt"
        model.fallbackMessage = ""
        #expect(model.validationMessage != nil)
        model.fallbackMessage = String(repeating: "x", count: 501)
        #expect(model.validationMessage != nil)
        model.fallbackMessage = "Sorry."
        model.systemPrompt = String(repeating: "x", count: 20_001)
        #expect(model.validationMessage != nil)
    }
}
