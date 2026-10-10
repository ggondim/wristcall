// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WristcallKit",
    platforms: [
        .watchOS("26.0"),
        .iOS("26.0"),
        .macOS("15.0"),
    ],
    products: [
        .library(name: "WristcallKit", targets: ["WristcallKit"]),
        // Test doubles (FakeTransport) for this package's tests and the app's tests. Not linked into the app.
        .library(name: "WristcallKitTesting", targets: ["WristcallKitTesting"]),
    ],
    targets: [
        .target(name: "WristcallKit"),
        .target(
            name: "WristcallKitTesting",
            dependencies: ["WristcallKit"]
        ),
        .testTarget(
            name: "WristcallKitTests",
            dependencies: ["WristcallKit", "WristcallKitTesting"]
        ),
        // Talks to a running wristcall server. Every test is skipped unless
        // WRISTCALL_TEST_SERVER is set (see watch/README.md).
        .testTarget(
            name: "WristcallKitIntegrationTests",
            dependencies: ["WristcallKit"]
        ),
    ]
)
