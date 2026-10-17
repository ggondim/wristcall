import SwiftUI
import Testing
import WristcallKit
@testable import Wristcall

struct AgentCellStyleTests {
    @Test func colorComesFromTheAgent() {
        let indigo = Agent(id: "a", slug: "a", displayName: "A", color: .indigo)
        #expect(AgentCellStyle.fill(for: indigo) == AgentColor.indigo.swiftUI)

        let none = Agent(id: "b", slug: "b", displayName: "B", color: nil)
        #expect(AgentCellStyle.fill(for: none) == AgentColor.mint.swiftUI)
    }

    @Test func unsupportedIsDimmed() {
        #expect(AgentCellStyle.opacity(supported: false) == 0.4)
        #expect(AgentCellStyle.opacity(supported: true) == 1)
        #expect(AgentCellStyle.diameter == 54)
    }
}
