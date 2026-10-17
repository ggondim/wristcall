import SwiftUI
import WristcallKit

/// Create (`editing == nil`) or edit an agent (mockup `iphone-05`): templates, name, color and icon, call type,
/// endpoint, server, voice and the rest under "Advanced". Opens as a sheet; it asks the server for its providers
/// itself, so every entry point only names the server.
struct AgentEditorView: View {
    let editing: AgentDetail?
    /// A model of the same server the caller already holds (its list refreshes with the save).
    let shared: AgentsModel?
    /// Runs after a save went through, before the sheet closes (the caller reloads what it shows).
    let onSaved: @MainActor (ManagedServer) async -> Void

    @Environment(AppState.self) private var state
    @Environment(\.dismiss) private var dismiss
    @State private var server: ManagedServer
    @State private var agents: AgentsModel?
    @State private var form: AgentFormModel?
    @State private var loadError: String?
    @State private var slugEdited = false
    @State private var saveError: String?
    @State private var saving = false
    @State private var moreIcons = false

    init(
        server: ManagedServer, editing: AgentDetail?, agents: AgentsModel? = nil,
        onSaved: @escaping @MainActor (ManagedServer) async -> Void
    ) {
        self.editing = editing
        shared = agents
        self.onSaved = onSaved
        _server = State(initialValue: server)
    }

    var body: some View {
        NavigationStack {
            Group {
                if let form {
                    editor(form)
                } else if let loadError {
                    ContentUnavailableView {
                        Label("Can't open the editor", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(loadError)
                    } actions: {
                        Button("Try again") { Task { await load(server, carrying: nil) } }
                    }
                } else {
                    ProgressView()
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle(editing?.displayName ?? "New agent")
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
                            .fontWeight(.semibold)
                            .disabled(!canSave)
                    }
                }
            }
        }
        .interactiveDismissDisabled(saving)
        .task { if form == nil { await load(server, carrying: nil) } }
    }

    private var canSave: Bool {
        guard let form else { return false }
        return form.validationMessage == nil && !(form.isEditing && form.fields().isEmpty)
    }

    // MARK: - Loading and saving

    /// Reads the providers of `target` and builds the form for it; `previous` (another server's form) hands over
    /// what the user already typed.
    private func load(_ target: ManagedServer, carrying previous: AgentFormModel?) async {
        loadError = nil
        let model: AgentsModel
        if let shared, shared.server.id == target.id {
            model = shared
        } else {
            model = AgentsModel(server: target, api: state.api(for: target)) { [state] list in
                await state.hooks.agentsChanged?(target, list)
            }
        }
        if model.providers == nil { await model.load() }
        guard let providers = model.providers else {
            loadError = model.error ?? "Can't reach the server."
            return
        }
        let next = AgentFormModel(editing: editing, providers: providers, serverVersion: state.healths[target.id]?.version)
        if let previous {
            next.carryOver(from: previous)
        } else if editing == nil {
            next.apply(.openAICompatible)
            next.slug = AgentFormModel.suggestSlug(from: next.displayName)
        }
        server = target
        agents = model
        form = next
    }

    private func save() async {
        guard let form, let agents else { return }
        saving = true
        saveError = nil
        defer { saving = false }
        if let message = await agents.save(form.fields(), editing: form.editing) {
            saveError = message
            return
        }
        await onSaved(server)
        dismiss()
    }

    // MARK: - Form

    @ViewBuilder
    private func editor(_ model: AgentFormModel) -> some View {
        @Bindable var form = model
        Form {
            if !form.isEditing {
                Section { templateChips(form) }
                    .listRowBackground(Color.clear)
                    .listRowInsets(EdgeInsets())
            }
            Section {
                HStack(spacing: 14) {
                    AgentBadge(icon: form.icon, color: form.color, size: 58)
                    TextField("Name", text: $form.displayName)
                        .font(.system(size: 20, weight: .semibold))
                        .padding(.horizontal, 16)
                        .frame(minHeight: 48)
                        .background(Color(.secondarySystemGroupedBackground), in: .capsule)
                }
            }
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 2, leading: 4, bottom: 2, trailing: 4))
            Section {
                VStack(spacing: 14) {
                    if form.supportsColor {
                        colorRow(form)
                        Divider()
                    }
                    iconGrid(form)
                }
                .padding(.vertical, 6)
            }
            Section {
                Picker("Call", selection: Binding(get: { form.callType }, set: { form.selectCallType($0) })) {
                    Text("Conversation").tag("conversation")
                    Text("One-shot").tag("one-shot")
                    Text("Monologue").tag("monologue")
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            } header: {
                Text("CALL")
            }
            if form.showsEndpoint { endpointSection(form) }
            if !form.isEditing && state.servers.count > 1 {
                Section {
                    Picker("Server", selection: Binding(get: { server.id }, set: { switchServer(to: $0, from: form) })) {
                        ForEach(state.servers) { Text($0.name).tag($0.id) }
                    }
                }
            }
            Section {
                if !form.isOneWay { voiceRow(form) }
                NavigationLink("Advanced") { AdvancedEditor(form: form, slugEdited: $slugEdited) }
            }
            if let message = form.validationMessage {
                Section { Text(message).font(.footnote).foregroundStyle(.secondary) }
            }
            if let saveError {
                Section { Text(saveError).foregroundStyle(.red) }
            }
            if let loadError {
                Section { Text(loadError).foregroundStyle(.red) }
            }
        }
        .onChange(of: form.displayName) { _, name in
            if !form.isEditing && !slugEdited { form.slug = AgentFormModel.suggestSlug(from: name) }
        }
        .sheet(isPresented: $moreIcons) {
            NavigationStack {
                Form { Section { IconPicker(selection: $form.icon) } }
                    .navigationTitle("Icon")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { moreIcons = false }.fontWeight(.semibold)
                        }
                    }
            }
            .presentationDetents([.medium, .large])
        }
    }

    private func switchServer(to id: String, from previous: AgentFormModel) {
        guard id != server.id, let target = state.servers.first(where: { $0.id == id }) else { return }
        Task { await load(target, carrying: previous) }
    }

    // MARK: Pieces

    private func templateChips(_ form: AgentFormModel) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(AgentTemplate.allCases, id: \.self) { template in
                    let on = form.template == template
                    Button { form.apply(template) } label: {
                        Text(template.title)
                            .font(.system(size: 14, weight: on ? .semibold : .regular))
                            .foregroundStyle(on ? Color.accentColor : Color.primary)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(on ? Color.accentColor.opacity(0.14) : Color(.secondarySystemGroupedBackground), in: .capsule)
                            .overlay { if on { Capsule().stroke(Color.accentColor, lineWidth: 1) } }
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .padding(.horizontal, 16)
        }
    }

    private func colorRow(_ form: AgentFormModel) -> some View {
        HStack(spacing: 0) {
            ForEach(AgentColor.allCases, id: \.self) { color in
                let on = AgentColor.resolved(form.color) == color
                Button { form.color = color } label: {
                    ZStack {
                        if on { Circle().stroke(color.swiftUI, lineWidth: 2.5).frame(width: 36, height: 36) }
                        Circle().fill(color.swiftUI).frame(width: 26, height: 26)
                    }
                    .frame(maxWidth: .infinity, minHeight: 40)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(color.rawValue.capitalized)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
    }

    private func iconGrid(_ form: AgentFormModel) -> some View {
        let names = IconPicker.suggested.contains(form.icon) ? IconPicker.suggested : [form.icon] + IconPicker.suggested
        return VStack(spacing: 10) {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 6), spacing: 10) {
                ForEach(names, id: \.self) { name in
                    let on = form.icon == name
                    Button { form.icon = name } label: {
                        Image(systemName: UIImage(systemName: name) == nil ? "questionmark" : name)
                            .font(.system(size: 19, weight: .medium))
                            .foregroundStyle(on ? Color.white : Color.primary.opacity(0.85))
                            .frame(width: 44, height: 44)
                            .background(on ? AgentColor.swiftUI(form.color) : Color(.tertiarySystemFill), in: .circle)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(name)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            HStack {
                Spacer()
                Button("More…") { moreIcons = true }.font(.subheadline).buttonStyle(.borderless)
            }
        }
    }

    private func choiceLabel(_ choice: AgentFormModel.EndpointChoice, _ form: AgentFormModel) -> String {
        switch choice {
        case .none: form.isEditing ? "Choose…" : "Server default"
        case .custom: "Custom"
        case .provider(let name): name
        }
    }

    // MARK: Endpoint

    private func endpointSection(_ model: AgentFormModel) -> some View {
        @Bindable var form = model
        return Section {
            if form.endpointHasPicker {
                Picker(form.isOneWay ? "Webhook" : "Responder", selection: $form.action) {
                    ForEach(form.choices(for: .action), id: \.self) { Text(choiceLabel($0, form)).tag($0) }
                }
            }
            if form.action == .custom {
                if form.isOneWay {
                    labeled("Webhook URL", width: 112) {
                        TextField("https://…", text: $form.webhookURL).keyboardType(.URL)
                    }
                    labeled("Header name", width: 112) {
                        TextField("Optional", text: $form.webhookHeaderName)
                    }
                    labeled("Header value", width: 112) {
                        SecureField("Optional", text: $form.webhookHeaderValue)
                    }
                } else {
                    labeled("URL", width: 76) {
                        TextField("https://api.openai.com/v1/chat/completions", text: $form.chatURL).keyboardType(.URL)
                    }
                    labeled("Model", width: 76) { TextField("gpt-4o-mini", text: $form.chatModel) }
                    labeled("API key", width: 76) { SecureField("sk-…", text: $form.chatAPIKey) }
                }
            }
        } header: {
            Text("AGENT ENDPOINT")
        } footer: {
            Text(form.isOneWay
                ? "Your server sends the transcript to this webhook."
                : "Your server turns your voice into text, sends it to this endpoint and speaks the answer.")
        }
    }

    private func labeled(_ title: String, width: CGFloat, @ViewBuilder field: () -> some View) -> some View {
        HStack(spacing: 8) {
            Text(title).frame(width: width, alignment: .leading)
            field()
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .lineLimit(1)
        }
    }

    private func voiceRow(_ model: AgentFormModel) -> some View {
        @Bindable var form = model
        // A custom voice service is set up under Advanced; here the providers of the server.
        let choices = form.choices(for: .tts).filter { $0 != .custom || form.tts == .custom }
        return Picker("Voice", selection: $form.tts) {
            ForEach(choices, id: \.self) { Text(choiceLabel($0, form)).tag($0) }
        }
    }
}

/// "Advanced": the fields the first screen leaves out.
private struct AdvancedEditor: View {
    @Bindable var form: AgentFormModel
    @Binding var slugEdited: Bool

    var body: some View {
        Form {
            Section("Agent") {
                if form.isEditing {
                    LabeledContent("Slug", value: form.slug)
                } else {
                    TextField("Slug", text: Binding(get: { form.slug }, set: { form.slug = $0; slugEdited = true }))
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                TextField("Language", text: $form.language)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Picker("Turn end", selection: $form.turnEnd) {
                    Text("Automatic").tag("auto")
                    Text("Manual").tag("manual")
                }
                Stepper("Silence: \(form.silenceMs) ms", value: $form.silenceMs, in: 100...10_000, step: 100)
                retentionFields
            }
            stageSection("Speech to text", .stt)
            if !form.isOneWay { stageSection("Voice service", .tts) }
            if !form.isOneWay {
                Section("Prompt") {
                    TextEditor(text: $form.systemPrompt).frame(minHeight: 100)
                    TextField(
                        "Failure message", text: $form.fallbackMessage,
                        prompt: Text("Sorry, I couldn't answer right now."), axis: .vertical
                    )
                }
            }
        }
        .navigationTitle("Advanced")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func label(_ choice: AgentFormModel.EndpointChoice) -> String {
        switch choice {
        case .none: form.isEditing ? "Choose…" : "Server default"
        case .custom: "Custom"
        case .provider(let name): name
        }
    }

    private func stageSection(_ title: String, _ stage: AgentFormModel.Stage) -> some View {
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
        return Section(title) {
            Picker(title, selection: selection) {
                ForEach(form.choices(for: stage), id: \.self) { Text(label($0)).tag($0) }
            }
            if form.choice(for: stage) == .custom {
                switch stage {
                case .stt: openAIFields(url: $form.sttBaseURL, model: $form.sttModel, key: $form.sttAPIKey)
                case .tts: openAIFields(url: $form.ttsBaseURL, model: $form.ttsModel, key: $form.ttsAPIKey)
                case .action: EmptyView()
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
    private var retentionFields: some View {
        let kind = Binding<Int>(
            get: {
                switch form.retention {
                case .serverDefault: 0
                case .days: 1
                case .forever: 2
                }
            },
            set: { value in
                switch value {
                case 1: form.retention = .days(30)
                case 2: form.retention = .forever
                default: form.retention = .serverDefault
                }
            }
        )
        Picker("Keep history", selection: kind) {
            Text("Server default").tag(0)
            Text("For some days").tag(1)
            Text("Forever").tag(2)
        }
        if case .days(let days) = form.retention {
            Stepper(
                "\(days) days",
                value: Binding(get: { days }, set: { form.retention = .days($0) }),
                in: 1...36_500
            )
        }
    }
}
