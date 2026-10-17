import Foundation
import SwiftUI
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct WatchStripTests {
    private let home = ManagedServer(id: "s1", name: "Home", url: URL(string: "https://home.example.com:8443")!, token: "wc_pat_a")
    private let work = ManagedServer(id: "s2", name: "Work", url: URL(string: "https://work.test")!, token: "wc_pat_b")
    private var homeCanonical: String { ServerAddress.canonical(home.url) }

    private func section(_ server: ManagedServer, agents: Int) -> AllAgentsModel.Section {
        .init(server: server, agents: (0..<agents).map { .sample(slug: "a\($0)") }, error: nil)
    }

    private func kind(
        paired: Bool = true, installed: Bool = true, watch: Set<String>, sections: [AllAgentsModel.Section],
        at: Date? = nil
    ) -> WatchStripState.Kind {
        WatchStripState.make(paired: paired, appInstalled: installed, watchServers: watch, sections: sections, lastContextAt: at)
    }

    @Test func hiddenWithoutAPairedWatch() {
        #expect(kind(paired: false, installed: false, watch: [homeCanonical], sections: [section(home, agents: 2)]) == .hidden)
        #expect(kind(paired: false, watch: [], sections: []) == .hidden)
    }

    @Test func opensOnWatchWhenTheWatchHasNoServers() {
        #expect(kind(watch: [], sections: [section(home, agents: 2)]) == .openOnWatch)
        #expect(kind(installed: false, watch: [], sections: [section(home, agents: 2)]) == .openOnWatch)
        #expect(kind(installed: false, watch: [], sections: []) == .openOnWatch)
    }

    @Test func countsOnlyAgentsOfServersOnTheWatch() {
        let at = Date(timeIntervalSince1970: 100)
        let sections = [section(home, agents: 3), section(work, agents: 4)]
        #expect(kind(watch: [homeCanonical], sections: sections, at: at) == .synced(agents: 3, at: at))
        #expect(kind(watch: [homeCanonical, ServerAddress.canonical(work.url)], sections: sections, at: at)
            == .synced(agents: 7, at: at))
    }

    @Test func readyWithoutAgents() {
        // No server on the iPhone yet.
        #expect(kind(watch: [], sections: []) == .ready)
        // The watch has the server, which has no agent (or none of the watch's servers loaded).
        #expect(kind(watch: [homeCanonical], sections: [section(home, agents: 0)]) == .ready)
        #expect(kind(watch: [homeCanonical], sections: [section(work, agents: 2)]) == .ready)
    }

    @Test func syncedTextSaysHowLongAgo() {
        let now = Date(timeIntervalSince1970: 10_000)
        #expect(WatchStripState.syncedText(agents: 3, at: now.addingTimeInterval(-5), now: now) == "3 agents · synced just now")
        #expect(WatchStripState.syncedText(agents: 1, at: now.addingTimeInterval(-120), now: now) == "1 agent · synced 2 minutes ago")
        #expect(WatchStripState.syncedText(agents: 2, at: now.addingTimeInterval(-7_200), now: now) == "2 agents · synced 2 hours ago")
        #expect(WatchStripState.syncedText(agents: 2, at: nil, now: now) == "2 agents")
    }

    @Test func badgeColorsAreTheSystemColors() {
        #expect(AgentColor.swiftUI(nil) == Color.mint)
        #expect(AgentColor.swiftUI(.red) == Color.red)
        #expect(AgentColor.swiftUI(.pink) == Color.pink)
        #expect(Set(AgentColor.allCases.map { "\($0.swiftUI)" }).count == AgentColor.allCases.count)
    }
}
