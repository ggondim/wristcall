import SwiftUI
import WristcallKit

/// What the strip at the top of the Agents home says about the Apple Watch.
struct WatchStripState: Equatable {
    enum Kind: Equatable {
        /// No Apple Watch is paired with this iPhone.
        case hidden
        /// Wristcall is not on the watch yet, or the watch has no server: the next step is on the watch.
        case openOnWatch
        /// The watch is set up and has no agent to show yet.
        case ready
        /// The watch has `agents` agents (those of its servers), as of `at`.
        case synced(agents: Int, at: Date?)
    }

    /// `watchServers` are `ServerAddress.canonical` addresses (the watch's application context); the agents
    /// counted are those of sections whose server is among them. `watchAccount` is the watch's account (C7):
    /// another account than `myAccount`, or none yet, means the next step is on the watch; a 0.4.0 watch
    /// (`.none`) keeps the rule of before, which only looks at its servers.
    static func make(
        paired: Bool, appInstalled: Bool, watchAccount: String??, myAccount: String?, watchServers: Set<String>,
        sections: [AllAgentsModel.Section], lastContextAt: Date?
    ) -> Kind {
        guard paired else { return .hidden }
        guard appInstalled else { return .openOnWatch }
        switch watchAccount {
        case .none:
            if watchServers.isEmpty && !sections.isEmpty { return .openOnWatch }
        case .some(let account):
            // Also while this iPhone's account is still starting (`myAccount == nil`): "open on watch" for those
            // few seconds, on purpose (C7).
            guard let account, account == myAccount else { return .openOnWatch }
        }
        let count = sections
            .filter { watchServers.contains(ServerAddress.canonical($0.server.url)) }
            .reduce(0) { $0 + $1.agents.count }
        return count == 0 ? .ready : .synced(agents: count, at: lastContextAt)
    }

    /// "3 agents · synced just now" ("1 agent", "synced 2 minutes ago"; no time when it is not known).
    static func syncedText(agents: Int, at: Date?, now: Date) -> String {
        let count = agents == 1 ? "1 agent" : "\(agents) agents"
        guard let at else { return count }
        let seconds = now.timeIntervalSince(at)
        if seconds < 60 { return "\(count) · synced just now" }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.unitsStyle = .full
        return "\(count) · synced \(formatter.localizedString(for: at, relativeTo: now))"
    }
}

/// The Apple Watch card (mockups `iphone-03` and `iphone-04`).
struct WatchStrip: View {
    let kind: WatchStripState.Kind

    var body: some View {
        switch kind {
        case .hidden:
            EmptyView()
        case .openOnWatch:
            card(title: "Open wristcall on your watch to finish", detail: nil, checked: false)
        case .ready:
            card(title: "Apple Watch ready", detail: Text("Agents show up there as you add them"), checked: true)
        case .synced(let agents, let at):
            // Re-read the clock every half minute: "just now" ages into "1 minute ago" on its own.
            TimelineView(.periodic(from: .now, by: 30)) { context in
                card(
                    title: "On your Apple Watch",
                    detail: Text(WatchStripState.syncedText(agents: agents, at: at, now: context.date)),
                    checked: true
                )
            }
        }
    }

    private func card(title: String, detail: Text?, checked: Bool) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "applewatch")
                .font(.system(size: 19, weight: .medium))
                .foregroundStyle(.white)
                .frame(width: 36, height: 36)
                .background(Color.accentColor, in: .circle)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body.weight(.semibold)).foregroundStyle(.primary)
                if let detail { detail.font(.subheadline).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 0)
            if checked {
                Image(systemName: "checkmark")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Color.accentColor)
                    .accessibilityHidden(true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}
