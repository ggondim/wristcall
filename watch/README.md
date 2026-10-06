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

The default `DESTINATION` is an Apple Watch Series 7 (45mm) simulator on watchOS 26.5, the one CI
uses. To create it: `python3 watch/scripts/ensure_simulator.py 'Apple Watch Series 7 (45mm)' 26.5`
(prints its UDID). To use another simulator, pass its UDID as in the last line above; list yours
with `xcrun simctl list devices watchOS`.

Integration tests talk to a local server with fake providers (no API keys,
config in `WristcallKit/Tests/test-server.yaml`). In one shell:

```sh
make -C watch test-server              # serves on http://127.0.0.1:8765
```

In another:

```sh
WRISTCALL_TEST_SERVER=http://127.0.0.1:8765 make -C watch test-integration
```

A plain `swift test` in `WristcallKit/` skips the integration suites when `WRISTCALL_TEST_SERVER`
is not set; `make -C watch test-integration` requires it and stops with an error without it.

## Run in the simulator

Open `watch/Wristcall.xcodeproj`, pick the `Wristcall` scheme and a watch simulator, and press Run.
No signing setup is needed for the simulator.

The simulator has no CallKit call UI, never activates the call's audio session and gets no
microphone, so a call there needs two launch arguments (Debug builds only; in Xcode add them under
Product > Scheme > Edit Scheme > Run > Arguments):

- `-noCallKit`: activates the audio session directly instead of going through CallKit.
- `-syntheticMic`: a generated tone replaces the microphone.

Typing the pairing code on the simulated watch is slow; `-pairServer <URL> -pairCode <8 digits>`
pairs at launch (without `-pairCode` it sends an approval request). From the command line, once
the app is installed (the bundle id changes if you set `BUNDLE_ID_PREFIX`):

```sh
xcrun simctl launch <UDID> io.github.ggondim.wristcall \
  -pairServer http://127.0.0.1:8765 -pairCode 12345678 -noCallKit -syntheticMic
```

`-autoCall` starts a call once the app is ready and `-endCallAfter <seconds>` ends it.

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
