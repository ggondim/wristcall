# wristcall watch app

Watch-only Apple Watch app that pairs with a wristcall server and calls your agent by voice
(protocol in [docs/protocol.md](../docs/protocol.md)).

- `WristcallKit/`: Swift package with everything that does not need the watch hardware
  (protocol, pairing, audio conversion, transport, call session). Runs with `swift test` on a Mac.
- `Wristcall/`: the watchOS app (SwiftUI, CallKit, audio). `WristcallTests/`: its unit tests.
- `project.yml`: XcodeGen spec. `Wristcall.xcodeproj` is generated and not committed.

## Prerequisites

- macOS with Xcode 26.6 or later (watchOS SDK 26+) and a watchOS 26 simulator runtime
  (Xcode > Settings > Components).
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) 2.46 or later: `brew install xcodegen`.
- For the integration tests: Python 3.12+ and the server from this repo
  (`python3 -m venv .venv && .venv/bin/pip install -e server`, then put `.venv/bin` on your `PATH`).

## Generate the project

```sh
make -C watch generate
open watch/Wristcall.xcodeproj
```

Run `make -C watch generate` again after pulling changes to `project.yml` or adding files.

## Test

```sh
make -C watch test-kit                 # WristcallKit unit tests on the Mac
make -C watch test-sim                 # app build + WristcallTests on a watch simulator
make -C watch test-sim DESTINATION='platform=watchOS Simulator,id=<UDID>'
```

The default `DESTINATION` is the maintainer's simulator; list yours with
`xcrun simctl list devices watchOS`. To create an Apple Watch Series 7 (45mm) on watchOS 26.5:
`python3 watch/scripts/ensure_simulator.py 'Apple Watch Series 7 (45mm)' 26.5` (prints its UDID).

Integration tests talk to a local server with fake providers (no API keys,
config in `WristcallKit/Tests/test-server.yaml`). In one shell:

```sh
make -C watch test-server              # serves on http://127.0.0.1:8765
```

In another:

```sh
WRISTCALL_TEST_SERVER=http://127.0.0.1:8765 make -C watch test-integration
```

Without `WRISTCALL_TEST_SERVER` the integration suites are skipped.

## Run in the simulator

Open `watch/Wristcall.xcodeproj`, pick the `Wristcall` scheme and a watch simulator, and press Run.
No signing setup is needed for the simulator.

## Install on your watch with a free Apple ID

A free Apple ID (Personal Team) is enough; no paid capability is used.

1. Add your Apple ID in Xcode > Settings > Accounts.
2. Find your team ID: open the generated project, select the `Wristcall` target >
   Signing & Capabilities, pick your Personal Team, then run
   `grep -m1 DEVELOPMENT_TEAM watch/Wristcall.xcodeproj/project.pbxproj`.
   (The next `make generate` discards that choice; the next step makes it permanent.)
3. Create `watch/Config/Local.xcconfig` (gitignored):

   ```
   DEVELOPMENT_TEAM = ABCDE12345
   BUNDLE_ID_PREFIX = com.example.yourname
   ```

   A bundle id belongs to a single team, so use a prefix of your own.
4. `make -C watch generate`, open the project, connect the iPhone paired with the watch,
   enable Developer Mode on the iPhone and on the watch (Settings > Privacy & Security)
   when asked, pick your watch as the run destination and press Run.
5. If the first launch is refused as an untrusted developer, trust your Apple ID under
   Settings > General > VPN & Device Management on the paired iPhone and run again.

Apps signed by a Personal Team expire after 7 days: run it from Xcode again to re-sign.
A free account can also register only a few app ids per week, so keep the same
`BUNDLE_ID_PREFIX` between installs.
