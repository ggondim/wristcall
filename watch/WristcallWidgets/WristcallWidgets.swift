import SwiftUI
import WidgetKit

/// The widget extension: a complication for watch faces and the Smart Stack, and a control for
/// Control Center (and the Action Button on Apple Watch Ultra). Both start a call in the app.
@main
struct WristcallWidgets: WidgetBundle {
    var body: some Widget {
        CallComplication()
        CallControl()
    }
}
