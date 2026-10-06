import Foundation

/// Settings for the integration tests, read from the environment.
///
/// - `WRISTCALL_TEST_SERVER`: base URL of a running wristcall server (e.g. `http://127.0.0.1:8765`).
///   Without it, every integration suite is skipped.
/// - `WRISTCALL_TEST_CONFIG`: the config file that server was started with. Needed by `runCLI`.
/// - `WRISTCALL_TEST_CLI`: the `wristcall` executable (default: `wristcall` from `PATH`).
enum TestServer {
    static let environment = ProcessInfo.processInfo.environment

    static let baseURL: URL? = environment["WRISTCALL_TEST_SERVER"].flatMap { URL(string: $0) }

    static var isConfigured: Bool { baseURL != nil }

    struct CLIError: Error, CustomStringConvertible {
        let description: String
    }

    #if os(macOS)
    /// Runs `wristcall <arguments> --config $WRISTCALL_TEST_CONFIG` and returns its standard output.
    static func runCLI(_ arguments: [String]) throws -> String {
        guard let config = environment["WRISTCALL_TEST_CONFIG"] else {
            throw CLIError(description: "WRISTCALL_TEST_CONFIG is not set")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = [environment["WRISTCALL_TEST_CLI"] ?? "wristcall"] + arguments + ["--config", config]
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(decoding: data, as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw CLIError(description: "wristcall \(arguments.joined(separator: " ")) exited with \(process.terminationStatus): \(text)")
        }
        return text
    }

    /// A fresh single-use pairing code from `wristcall pair`, as 8 digits.
    static func newPairingCode() throws -> String {
        let output = try runCLI(["pair"])
        guard let line = output.split(separator: "\n").first(where: { $0.hasPrefix("Pairing code:") }) else {
            throw CLIError(description: "no pairing code in: \(output)")
        }
        let digits = line.filter(\.isNumber)
        guard digits.count == 8 else {
            throw CLIError(description: "unexpected pairing code line: \(line)")
        }
        return String(digits)
    }
    #endif
}
