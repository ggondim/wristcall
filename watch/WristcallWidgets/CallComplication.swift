import SwiftUI
import WidgetKit
import WristcallKit

/// A complication that opens the app on `wristcall://call`; the app starts the call
/// (`ShortcutLink`, `PendingCallStore`). Nothing to refresh: one entry, never reloaded.
struct CallComplication: Widget {
    static let kind = "io.github.ggondim.wristcall.call-complication"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: Self.kind, provider: CallComplicationProvider()) { _ in
            CallComplicationView()
                .widgetURL(ShortcutLink.call)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Call agent")
        .description("Calls your agent with Wristcall.")
        .supportedFamilies([.accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}

struct CallComplicationEntry: TimelineEntry {
    let date: Date
}

struct CallComplicationProvider: TimelineProvider {
    func placeholder(in context: Context) -> CallComplicationEntry {
        CallComplicationEntry(date: .now)
    }

    func getSnapshot(in context: Context, completion: @escaping (CallComplicationEntry) -> Void) {
        completion(CallComplicationEntry(date: .now))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CallComplicationEntry>) -> Void) {
        completion(Timeline(entries: [CallComplicationEntry(date: .now)], policy: .never))
    }
}

struct CallComplicationView: View {
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryCorner:
            Image(systemName: "phone.fill")
                .font(.title2)
                .widgetLabel("Call agent")
        case .accessoryInline:
            Label("Call agent", systemImage: "phone.fill")
        default:
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: "phone.fill")
                    .font(.title2)
            }
            .accessibilityLabel("Call agent")
        }
    }
}

#Preview("Circular", as: .accessoryCircular) {
    CallComplication()
} timeline: {
    CallComplicationEntry(date: .now)
}

#Preview("Corner", as: .accessoryCorner) {
    CallComplication()
} timeline: {
    CallComplicationEntry(date: .now)
}
