import Testing
@testable import Wristcall

struct AgentIconTests {
    @Test func aKnownSymbolIsKept() {
        #expect(AgentIcon.symbolName(for: "note.text") == "note.text")
        #expect(AgentIcon.symbolName(for: "waveform") == "waveform")
    }

    @Test func anUnknownNameFallsBackToWaveform() {
        #expect(AgentIcon.symbolName(for: "no.such.symbol") == "waveform")
    }

    @Test func anEmptyOrBlankNameFallsBackToWaveform() {
        #expect(AgentIcon.symbolName(for: "") == "waveform")
        #expect(AgentIcon.symbolName(for: "   ") == "waveform")
    }
}
