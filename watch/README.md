# wristcall watch app

Watch-only Apple Watch app that pairs with one or more wristcall servers and calls your agents by voice
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

`-autoCall` starts a call once the app is ready and `-endCallAfter <seconds>` ends it. More Debug
launch arguments:

- `-autoCallAgent <slug>`: the agent `-autoCall` calls (default: the first one). If no listed agent has that
  slug it calls nothing, never another agent.
- `-autoCallDelay <seconds>`: waits on the agent grid before `-autoCall` calls.
- `-showOptions <slug>`: opens that agent's call options, as a long press on it does.
- `-addServer`: opens the pairing screen over the servers already paired. `-pairServer` (above) also adds a
  server when the watch is already paired, unless that server is listed.
- `-openURL <url>`: does what a tapped complication does, for example
  `-openURL 'wristcall://call?agent=<server id>/<agent id>'`. `xcrun simctl openurl` fails on watch simulators
  (LaunchServices error 115), so use this one.

## Servers, agents and calls

- **Several servers.** Settings > Servers lists the paired servers. "Add server" opens the pairing screen again
  (cancel to go back) and pairing the same server and user again replaces the old entry and revokes its token.
  Removing a server forgets its token on the watch and tries to revoke it on the server (if the server is out of
  reach, revoke the watch there). A watch paired with 0.1.0 keeps its server when it updates.
- **Agent grid.** Home shows the agents of every server, two per row, in each server's order. Tap an agent to
  call it; long press opens its call options (end of turn, auto or manual, for conversations). The "…" button in
  the toolbar opens the same options: directly for the only callable agent, or a list of agents to pick from.
  Messages ("No connection", "Agent not found.") show at the top, above the grid. A server that is
  loading or out of reach shows as a row of its own with "Retry", and the agents of the other servers stay
  callable. A server that rejects the token (revoked) is removed on its own.
- **Call types.** A conversation agent works as before (CallKit call screen, mute from the system). A one-shot or
  monologue agent uses the same call but the screen says "Recording" ("Paused" while muted) and "Send" ends it
  ("Send" while still "Connecting…" records nothing and Home says "Nothing was sent.").
  The server then transcribes and delivers the text, and the watch shows a progress ring while it works, then a
  check ("Delivered") or a cross with the reason, plus the text.
- **Results are polled while the app is open.** The watch asks the server about the call (`GET /v1/calls/{id}`)
  every 1.5 s for up to 3 minutes, asks again when the app comes back to the foreground, and offers "Check again"
  after that. A call without a final status is remembered (call id, server and agent ids, time; no text) for 24
  hours: the next time the app opens it asks again. Tapping a complication, control or shortcut while the result
  screen says "Sending…" closes it and the outcome is not shown (it stays in the server's history). In the push
  build (below), a notification ends the wait at once.
- **Servers older than 0.4.0.** With servers 0.2.x each profile appears as one conversation agent. Servers 0.3.0
  and later list real agents and turn modes, and 0.4.0 and later also record one-way agents.

## Push notifications (push build)

Push needs the `aps-environment` entitlement, which a free Apple ID (Personal Team) cannot sign. So the push code is
built only in the `DebugPush` configuration (`WRISTCALL_PUSH`, `Config/Push.xcconfig`,
`Wristcall/Wristcall-Push.entitlements`); Debug and Release are as before and never ask for notifications. On a real
watch the push build needs a paid Apple Developer account, and the relay (the wristcall Cloud) needs that account's
APNs key; until then it is a simulator build. Servers need 0.6.0 with `push` configured: with servers 0.5.0 and older
the watch polls the result as before.

**Privacy.** The notification's title, body and label (the server's host) pass through the relay and Apple. The
transcript never does: the watch reads it from the server when it shows the result.

```sh
make -C watch build-sim-push DESTINATION='platform=watchOS Simulator,id=<UDID>'
```

- **Relay.** `WRISTCALL_RELAY_URL` in `Config/Push.xcconfig` (default `http://127.0.0.1:8090`, a local Cloud) and
  `WRISTCALL_PUSH_ENVIRONMENT` (`sandbox`) become the `Info.plist` keys `WristcallRelayURL` and
  `WristcallPushEnvironment`. Without a relay push is off. The watch never uses a relay a server announces: a
  server whose `/v1/health` names another relay (or none) is skipped.
- **One key per server.** At launch the watch asks APNs for a token, registers it at the relay once per paired
  server (label: the server's host, which the relay shows on every notification of that key; tag: the server's
  local id, which comes back in every push) and hands the key to that server (`PUT /v1/push`). The key and the
  token are kept in the Keychain (account `push.<server id>`). Every time the app comes to the foreground it checks
  each key: a key the relay forgot is registered again, a new APNs token replaces the old keys, and the server gets
  the key again. A server or relay without push (`404`) is skipped silently; a failure on one server does not stop
  the others.
- **Removing a server** clears its key on the server first (`DELETE /v1/push`, while the token still works), then
  revokes the watch (`DELETE /v1/me`), then drops the key at the relay (best effort) and from the Keychain.
- **Notifications.** The watch asks for permission when the first one-way result shows, not at the first launch. A
  `call.finished` notification for the result on screen ends its wait without a banner; any other one shows a
  banner, and tapping it opens that call's result.
- **Simulator.** The watch simulator gets no APNs token: the launch argument `-WCFakeAPNsToken <hex>` (Debug builds,
  at least 32 bytes for the Cloud) stands in for it. `xcrun simctl push <UDID> <bundle id> payload.json` delivers a
  notification, for example:

  ```json
  {"aps": {"alert": {"title": "Delivered", "subtitle": "127.0.0.1", "body": "Notes got your message."}},
   "wristcall": {"v": 1, "event": "call.finished", "tag": "<server id>",
                 "data": {"call_id": "<call id>", "status": "delivered", "error": null, "agent_id": "<agent id>"}}}
  ```

## Complications, controls and shortcuts

The "Call <agent>" complication, control and shortcut (watchOS 26) call the agent you picked when setting them up.
Until you pick one, the new complication says "Choose agent" and the new control only opens the app. The 0.1.0
complication, control and "Call agent" shortcut stay as they were and call the first agent: the first agent of the
first server in Settings > Servers. If that server is out of reach the watch says "Can't reach <host>." and calls
nobody, never an agent of the next server. An agent that no longer exists (server removed, agent deleted) never
turns into a call to another one: the item shows "Agent not found" and tapping it says so. Redialing a call from
the system's call history calls the agent with that name only when no other agent has the same name.

They read the list of agents from an App Group (`group.<BUNDLE_ID_PREFIX>.wristcall`) shared by the app and the widgets
extension. For each agent the list holds the server's local id, the agent id, slug, name, icon, call type and the
server host, never a token. The group is required to install: on the first build for your watch, Xcode's automatic
signing must register it for the app and for the widgets extension in your team. Open the project, select each
target > Signing & Capabilities and check that "App Groups" shows the group without errors, then build again.
If Xcode cannot register it, signing fails and nothing installs. As a fallback, remove the two
`CODE_SIGN_ENTITLEMENTS` lines from `project.yml`, run `make generate` and install without the App Group: the new
complication and control still appear in the gallery, but they find no agents to pick, while the 0.1.0 ones keep
working.

## Install on your watch with a free Apple ID

A free Apple ID (Personal Team) is enough. App Groups work with a Personal Team and are the one capability to
register (for both the app and the widgets extension).

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
