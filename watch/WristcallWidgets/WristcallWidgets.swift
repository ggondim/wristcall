import SwiftUI
import WidgetKit

/// The widget extension: complications for watch faces and the Smart Stack, and controls for
/// Control Center (and the Action Button on Apple Watch Ultra). All start a call in the app: the
/// first agent, or the one the person picked (configurable, decision W12).
@main
struct WristcallWidgets: WidgetBundle {
    var body: some Widget {
        CallComplication()
        CallControl()
        AgentCallComplication()
        AgentCallControl()
    }
}
