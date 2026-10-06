import Testing
@testable import Wristcall

@MainActor
struct HomeViewTests {
    @Test func titleComesFromWristcallKit() {
        #expect(HomeView.title == "wristcall")
    }
}
