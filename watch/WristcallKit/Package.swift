// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WristcallKit",
    platforms: [
        .watchOS("26.0"),
        .macOS("15.0"),
    ],
    products: [
        .library(name: "WristcallKit", targets: ["WristcallKit"]),
    ],
    targets: [
        .target(name: "WristcallKit"),
        .testTarget(
            name: "WristcallKitTests",
            dependencies: ["WristcallKit"]
        ),
        // Talks to a running wristcall server. Every test is skipped unless
        // WRISTCALL_TEST_SERVER is set (see watch/README.md).
        .testTarget(
            name: "WristcallKitIntegrationTests",
            dependencies: ["WristcallKit"]
        ),
    ]
)
