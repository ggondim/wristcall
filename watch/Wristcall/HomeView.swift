import SwiftUI
import WristcallKit

struct HomeView: View {
    static let title = WristcallKitInfo.name

    var body: some View {
        Text(Self.title)
    }
}

#Preview {
    HomeView()
}
