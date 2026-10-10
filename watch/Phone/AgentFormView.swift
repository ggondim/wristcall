import SwiftUI
import WristcallKit

/// Create (`editing == nil`) or edit an agent. Saves through `AgentsModel`, which reloads the list.
struct AgentFormView: View {
    let agents: AgentsModel
    @Environment(\.dismiss) private var dismiss
    @State private var form: AgentFormModel
    @State private var slugEdited = false
    @State private var advanced = false
    @State private var saveError: String?
    @State private var saving = false

    init(editing: AgentDetail?, model: AgentsModel) {
        agents = model
        _form = State(initialValue: AgentFormModel(
            editing: editing,
            providers: model.providers ?? ProviderList(providers: [], customEndpoints: false)
        ))
    }

    var body: some View {
        @Bindable var form = form
        NavigationStack {
            Form {
                Section("Agent") {
                    TextField("Name", text: $form.displayName)
                    if form.isEditing {
                        LabeledContent("Slug", value: form.slug)
                    } else {
                        TextField("Slug", text: Binding(get: { form.slug }, set: { form.slug = $0; slugEdited = true }))
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    }
                }
                Section("Icon") { IconPicker(selection: $form.icon) }
                Section {
                    Picker("Call type", selection: Binding(get: { form.callType }, set: { form.selectCallType($0) })) {
                        Text("Conversation").tag("conversation")
                        Text("One-shot").tag("one-shot")
                        Text("Monologue").tag("monologue")
                    }
                } footer: {
                    Text(form.isOneWay
                        ? "The watch records you and sends the transcript to a webhook. There is no answer and no voice."
                        : "You talk and the agent answers with a voice.")
                }
                stageSection("Speech to text", .stt, form: form)
                stageSection(form.isOneWay ? "Webhook" : "Responder", .action, form: form)
                if !form.isOneWay { stageSection("Voice", .tts, form: form) }
                Section { DisclosureGroup("Advanced", isExpanded: $advanced) { advancedFields($form) } }
                if let message = form.validationMessage {
                    Section { Text(message).font(.footnote).foregroundStyle(.secondary) }
                }
                if let saveError {
                    Section { Text(saveError).foregroundStyle(.red) }
                }
            }
            .navigationTitle(form.isEditing ? "Edit agent" : "New agent")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if saving {
                        ProgressView()
                    } else {
                        Button("Save") { Task { await save() } }
                            .disabled(form.validationMessage != nil || (form.isEditing && form.fields().isEmpty))
                    }
                }
            }
            .interactiveDismissDisabled(saving)
            .onChange(of: form.displayName) { _, name in
                if !form.isEditing && !slugEdited { form.slug = AgentFormModel.suggestSlug(from: name) }
            }
        }
    }

    // MARK: Pieces

    private func label(_ choice: AgentFormModel.EndpointChoice) -> String {
        switch choice {
        case .none: form.isEditing ? "Choose…" : "Server default"
        case .custom: "Custom"
        case .provider(let name): name
        }
    }

    @ViewBuilder
    private func stageSection(_ title: String, _ stage: AgentFormModel.Stage, form: AgentFormModel) -> some View {
        @Bindable var form = form
        let selection = Binding<AgentFormModel.EndpointChoice>(
            get: { form.choice(for: stage) },
            set: { value in
                switch stage {
                case .stt: form.stt = value
                case .action: form.action = value
                case .tts: form.tts = value
                }
            }
        )
        Section {
            Picker(title, selection: selection) {
                ForEach(form.choices(for: stage), id: \.self) { Text(label($0)).tag($0) }
            }
            if form.choice(for: stage) == .custom {
                if stage == .action && form.isOneWay {
                    TextField("https://…", text: $form.webhookURL, axis: .vertical)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    TextField("Header name (optional)", text: $form.webhookHeaderName)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Header value", text: $form.webhookHeaderValue)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } else {
                    switch stage {
                    case .stt: openAIFields(url: $form.sttBaseURL, model: $form.sttModel, key: $form.sttAPIKey)
                    case .action: openAIFields(url: $form.chatBaseURL, model: $form.chatModel, key: $form.chatAPIKey)
                    case .tts: openAIFields(url: $form.ttsBaseURL, model: $form.ttsModel, key: $form.ttsAPIKey)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func openAIFields(url: Binding<String>, model: Binding<String>, key: Binding<String>) -> some View {
        TextField("Base URL (https://…/v1)", text: url, axis: .vertical)
            .keyboardType(.URL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
        TextField("Model", text: model)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
        SecureField("API key (optional)", text: key)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
    }

    @ViewBuilder
    private func advancedFields(_ form: Bindable<AgentFormModel>) -> some View {
        TextField("Language", text: form.language)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
        Picker("Turn end", selection: form.turnEnd) {
            Text("Automatic").tag("auto")
            Text("Manual").tag("manual")
        }
        Stepper("Silence: \(form.wrappedValue.silenceMs) ms", value: form.silenceMs, in: 100...10_000, step: 100)
        retentionFields(form)
        if !form.wrappedValue.isOneWay {
            VStack(alignment: .leading) {
                Text("Prompt").font(.footnote).foregroundStyle(.secondary)
                TextEditor(text: form.systemPrompt).frame(minHeight: 100)
            }
            TextField("Failure message", text: form.fallbackMessage, prompt: Text("Sorry, I couldn't answer right now."), axis: .vertical)
        }
    }

    @ViewBuilder
    private func retentionFields(_ form: Bindable<AgentFormModel>) -> some View {
        let kind = Binding<Int>(
            get: {
                switch form.wrappedValue.retention {
                case .serverDefault: 0
                case .days: 1
                case .forever: 2
                }
            },
            set: { value in
                switch value {
                case 1: form.wrappedValue.retention = .days(30)
                case 2: form.wrappedValue.retention = .forever
                default: form.wrappedValue.retention = .serverDefault
                }
            }
        )
        Picker("Keep history", selection: kind) {
            Text("Server default").tag(0)
            Text("For some days").tag(1)
            Text("Forever").tag(2)
        }
        if case .days(let days) = form.wrappedValue.retention {
            Stepper(
                "\(days) days",
                value: Binding(get: { days }, set: { form.wrappedValue.retention = .days($0) }),
                in: 1...36_500
            )
        }
    }

    private func save() async {
        saving = true
        saveError = nil
        defer { saving = false }
        if let message = await agents.save(form.fields(), editing: form.editing) {
            saveError = message
        } else {
            dismiss()
        }
    }
}
