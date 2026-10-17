import SwiftUI
import WristcallKit

/// "Sign in on the web?" for a web app QR code (spec O5): the Cloud describes the browser behind the code,
/// then this iPhone approves it. The approve key never reaches the screen or a log.
@MainActor
@Observable
final class WebApprovalModel {
    enum Phase: Equatable {
        /// Waiting for the account, or for the Cloud's description.
        case loading
        case ready(WebSessionInfo)
        case approving(WebSessionInfo)
        case approved
        case failed(String)
    }

    let id: String
    @ObservationIgnored private let key: String
    @ObservationIgnored let account: AccountModel
    private(set) var phase: Phase = .loading

    init(id: String, key: String, account: AccountModel) {
        self.id = id
        self.key = key
        self.account = account
    }

    var info: WebSessionInfo? {
        switch phase {
        case .ready(let info), .approving(let info): info
        default: nil
        }
    }

    /// Asks the Cloud what the code stands for (once the account is ready).
    func load() async {
        guard account.state == .ready, phase == .loading else { return }
        do {
            phase = .ready(try await account.inspectWebSession(id: id, key: key))
        } catch {
            phase = .failed(AccountModel.webMessage(for: error))
        }
    }

    /// Approves the sign-in. `true` once the Cloud took it.
    @discardableResult
    func approve() async -> Bool {
        guard case .ready(let info) = phase else { return false }
        phase = .approving(info)
        do {
            try await account.approveWebSession(id: id, key: key)
            phase = .approved
            return true
        } catch {
            phase = .failed(AccountModel.webMessage(for: error))
            return false
        }
    }

    // MARK: - Text

    /// The page picks its own name: shown quoted, on one line.
    static func pageText(_ name: String) -> String {
        "Page says: “\(oneLine(name))”"
    }

    static func browserText(_ userAgent: String?) -> String {
        guard let userAgent, !oneLine(userAgent).isEmpty else { return "Unknown browser" }
        return oneLine(userAgent)
    }

    /// ISO 3166 code → the country's name in `locale`; `nil` → "Unknown location".
    static func countryText(_ code: String?, locale: Locale = .current) -> String {
        guard let code, !code.isEmpty else { return "Unknown location" }
        return locale.localizedString(forRegionCode: code) ?? code.uppercased()
    }

    static func openedText(_ date: Date, now: Date = Date(), locale: Locale = .current) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = locale
        formatter.unitsStyle = .full
        return "Opened \(formatter.localizedString(for: min(date, now), relativeTo: now))"
    }

    /// Line breaks and runs of spaces become one space.
    private static func oneLine(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
    }
}

struct WebApproveView: View {
    @State private var model: WebApprovalModel
    @Environment(LinkRouter.self) private var router

    /// The router's token for this sheet: closing never closes a newer one.
    private let token: Int

    init(id: String, key: String, account: AccountModel, token: Int) {
        _model = State(initialValue: WebApprovalModel(id: id, key: key, account: account))
        self.token = token
    }

    static let warning = "Only approve a code you opened yourself."

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                Image(systemName: "macbook.and.iphone")
                    .font(.system(size: 28, weight: .medium))
                    .foregroundStyle(.white)
                    .frame(width: 64, height: 64)
                    .background(Color(white: 0.08), in: .rect(cornerRadius: 16))
                    .accessibilityHidden(true)
                    .padding(.top, 28)
                Text("Sign in on the web?")
                    .font(.title2.bold())
                    .padding(.top, 16)
                content
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(.systemGroupedBackground))
        .interactiveDismissDisabled(isApproving)
        .task(id: model.account.state == .ready) { await model.load() }
    }

    @ViewBuilder
    private var content: some View {
        switch model.phase {
        case .loading:
            VStack(spacing: 10) {
                ProgressView()
                if let note = accountNote { Text(note).font(.callout).foregroundStyle(.secondary) }
            }
            .padding(.top, 32)
            cancelButton
        case .failed(let text):
            Text(text)
                .font(.callout)
                .foregroundStyle(.red)
                .multilineTextAlignment(.center)
                .padding(.top, 24)
            Button("Done") { router.close(token) }
                .font(.headline)
                .padding(.top, 24)
        case .approved:
            EmptyView()
        case .ready(let info), .approving(let info):
            details(info)
        }
    }

    private func details(_ info: WebSessionInfo) -> some View {
        VStack(spacing: 0) {
            Text(verbatim: WebApprovalModel.pageText(info.name))
                .lineLimit(1)
                .truncationMode(.tail)
                .foregroundStyle(.secondary)
                .padding(.top, 4)
            VStack(spacing: 0) {
                row("Browser", WebApprovalModel.browserText(info.userAgent))
                Divider().padding(.leading, 16)
                row("Location", WebApprovalModel.countryText(info.country))
                Divider().padding(.leading, 16)
                HStack {
                    Text(WebApprovalModel.openedText(info.createdAt)).foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
            .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 22))
            .padding(.top, 24)

            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "shield").foregroundStyle(.orange).accessibilityHidden(true)
                Text(Self.warning)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, 16)
            .padding(.horizontal, 4)

            Spacer(minLength: 28)
            if isApproving {
                ProgressView().controlSize(.large).frame(height: 50)
            } else {
                Button {
                    Task { if await model.approve() { router.close(token) } }
                } label: {
                    Text("Approve").font(.headline).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.capsule)
                .controlSize(.large)
                .padding(.top, 8)
            }
            cancelButton.disabled(isApproving)
        }
    }

    private var cancelButton: some View {
        Button("Cancel") { router.close(token) }
            .font(.headline)
            .padding(.top, 14)
    }

    private var isApproving: Bool {
        if case .approving = model.phase { return true }
        return false
    }

    private var accountNote: String? { model.account.linkWaitNote }

    private func row(_ title: String, _ value: String) -> some View {
        HStack(spacing: 12) {
            Text(title)
            Spacer(minLength: 8)
            Text(verbatim: value)
                .lineLimit(1)
                .truncationMode(.tail)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
    }
}
