import Foundation
import Testing
import WristcallKit

struct JSONValueTests {
    private func decode(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    private func encode(_ value: JSONValue) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return String(decoding: try encoder.encode(value), as: UTF8.self)
    }

    @Test func decodesIntBeforeDouble() throws {
        let value = try decode(#"{"a":30,"b":0.5}"#)
        #expect(value["a"] == .int(30))
        #expect(value["b"] == .double(0.5))
    }

    @Test func decodesEveryKind() throws {
        let value = try decode(#"{"n":null,"t":true,"s":"x","l":[1,"a"],"o":{"k":false}}"#)
        #expect(value["n"] == .null)
        #expect(value["t"] == .bool(true))
        #expect(value["s"] == .string("x"))
        #expect(value["l"] == .array([.int(1), .string("a")]))
        #expect(value["o"] == .object(["k": .bool(false)]))
    }

    @Test func encodesIntWithoutFraction() throws {
        #expect(try encode(.int(30)) == "30")
    }

    @Test func roundTripsNestedObject() throws {
        let value = JSONValue.object([
            "provider": .string("openai"),
            "options": .object(["base_url": .string("https://x.test/v1"), "max_tokens": .int(512), "temperature": .double(0.25)]),
            "stop": .array([.string("a"), .null, .bool(true)]),
        ])
        #expect(try decode(try encode(value)) == value)
    }

    @Test func nullAndMissingDiffer() throws {
        let fields: [String: JSONValue] = ["tts": .null]
        let data = try JSONEncoder().encode(fields)
        #expect(String(decoding: data, as: UTF8.self) == #"{"tts":null}"#)
        #expect(try JSONEncoder().encode([String: JSONValue]()) == Data("{}".utf8))
    }

    @Test func accessorsReturnNilForOtherKinds() {
        #expect(JSONValue.string("a").stringValue == "a")
        #expect(JSONValue.int(3).stringValue == nil)
        #expect(JSONValue.int(3).intValue == 3)
        #expect(JSONValue.double(3).intValue == nil)
        #expect(JSONValue.string("a")["k"] == nil)
        #expect(JSONValue.object([:])["k"] == nil)
    }
}
