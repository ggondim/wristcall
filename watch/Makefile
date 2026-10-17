# wristcall watch app: generate, build and test. Run from watch/ or with `make -C watch <target>`.

PROJECT := Wristcall.xcodeproj
SCHEME := Wristcall
# Any xcodebuild destination. The default is the simulator CI creates (scripts/ensure_simulator.py);
# to use another, pass DESTINATION='platform=watchOS Simulator,id=<UDID>'
# (list yours with `xcrun simctl list devices watchOS`).
DESTINATION ?= platform=watchOS Simulator,name=Apple Watch Series 7 (45mm),OS=26.5
DERIVED_DATA := build/DerivedData
# Simulator builds are signed ad hoc ("Sign to Run Locally"; no team or certificate needed).
# Do not pass CODE_SIGNING_ALLOWED=NO: an unsigned app gets errSecMissingEntitlement (-34018) from the Keychain.
XCODEBUILD := xcodebuild -project $(PROJECT) -scheme $(SCHEME) -destination '$(DESTINATION)' \
	-derivedDataPath $(DERIVED_DATA)

# The iPhone app (embeds the watch app). Any iOS simulator destination, for example
# IOS_DESTINATION='platform=iOS Simulator,id=<UDID>' (list yours with `xcrun simctl list devices iOS`).
IOS_SCHEME := WristcallPhone
IOS_DESTINATION ?= platform=iOS Simulator,name=iPhone 17
XCODEBUILD_IOS := xcodebuild -project $(PROJECT) -scheme $(IOS_SCHEME) -destination '$(IOS_DESTINATION)' \
	-derivedDataPath $(DERIVED_DATA)

# Cloud and push relay URLs reach every build when given (`make build-ios CLOUD_URL=http://127.0.0.1:8090`);
# left out, Config/Local.xcconfig or else Config/Base.xcconfig applies (the project's production Cloud, no relay).
CLOUD_URL ?=
RELAY_URL ?=
URL_SETTINGS := $(if $(CLOUD_URL),WRISTCALL_CLOUD_URL=$(CLOUD_URL)) $(if $(RELAY_URL),WRISTCALL_RELAY_URL=$(RELAY_URL))

# Integration tests (see README.md): a local wristcall server with fake providers.
TEST_SERVER_PORT ?= 8765
export WRISTCALL_TEST_CONFIG ?= $(CURDIR)/WristcallKit/Tests/test-server.yaml
export WRISTCALL_TEST_DATA_DIR ?= $(CURDIR)/build/test-server-data
export WRISTCALL_TEST_CLI ?= wristcall

.PHONY: generate test-kit test-server test-integration test-account build build-sim-push test-sim build-ios build-ios-push test-ios test-ios-ui shots-ios clean

generate:
	xcodegen generate --spec project.yml

test-kit:
	cd WristcallKit && swift test --skip WristcallKitIntegrationTests

# Starts the test server in the foreground (needs `pip install -e server`); run the tests from another shell.
test-server:
	WRISTCALL_TEST_SERVER=http://127.0.0.1:$(TEST_SERVER_PORT) $(WRISTCALL_TEST_CLI) serve \
		--config "$(WRISTCALL_TEST_CONFIG)" --host 127.0.0.1 --port $(TEST_SERVER_PORT)

# Starts nothing: expects WRISTCALL_TEST_SERVER to point at a running server.
test-integration:
	@test -n "$(WRISTCALL_TEST_SERVER)" || { echo "error: set WRISTCALL_TEST_SERVER, e.g. http://127.0.0.1:$(TEST_SERVER_PORT) (see README.md)"; exit 1; }
	cd WristcallKit && swift test --filter WristcallKitIntegrationTests

# Account flows end to end (AccountIntegrationTests): a local Cloud, a server linked to it and a real OIDC provider
# with a browser helper (tools/e2e/zitadel_browser.py). Starts nothing; a script sets WRISTCALL_TEST_CLOUD,
# WRISTCALL_TEST_SERVER, WRISTCALL_TEST_CONFIG, WRISTCALL_TEST_BROWSER and WRISTCALL_TEST_FAKE_APNS_LOG.
test-account:
	@test -n "$(WRISTCALL_TEST_CLOUD)" || { echo "error: set WRISTCALL_TEST_CLOUD and the other account settings (see README.md)"; exit 1; }
	cd WristcallKit && swift test --filter AccountIntegrationTests

build: generate
	$(XCODEBUILD) build $(URL_SETTINGS)

# The push build (DebugPush: WRISTCALL_PUSH, aps-environment, relay from Config/Push.xcconfig) for
# the simulator. A free Apple ID cannot sign aps-environment for a real watch; see README.md.
build-sim-push: generate
	$(XCODEBUILD) -configuration DebugPush build $(URL_SETTINGS)

test-sim: generate
	$(XCODEBUILD) test $(URL_SETTINGS)

# The iPhone app with the watch app embedded (Wristcall.app/Watch/).
build-ios: generate
	$(XCODEBUILD_IOS) build $(URL_SETTINGS)

build-ios-push: generate
	$(XCODEBUILD_IOS) -configuration DebugPush build $(URL_SETTINGS)

test-ios: generate
	$(XCODEBUILD_IOS) test $(URL_SETTINGS)

# The UI targets refuse to run without an explicit CLOUD_URL: the app makes a silent account on whatever Cloud
# the build names, and the default build names the production Cloud (an orphan account on every clean run).
REQUIRE_CLOUD_URL = @test -n "$(CLOUD_URL)" || { \
	echo "$@: set CLOUD_URL=<url> (a local Cloud such as http://127.0.0.1:8090): the app makes a silent account there,"; \
	echo "$@: and without it the build talks to the production Cloud."; exit 1; }

# UI smoke test (XCUITest, own scheme WristcallPhoneUI, never in CI): needs a Cloud (CLOUD_URL, required), the
# test server running (`make test-server`) and a personal token of it, passed with the TEST_RUNNER_ prefix:
#   CLOUD_URL=http://127.0.0.1:8090 TEST_RUNNER_WRISTCALL_UI_TOKEN=wc_pat_... make test-ios-ui
# TEST_RUNNER_WRISTCALL_UI_SHOTS=<folder> also writes the screenshots there. The test skips without a token.
test-ios-ui: generate
	$(REQUIRE_CLOUD_URL)
	xcodebuild -project $(PROJECT) -scheme WristcallPhoneUI -destination '$(IOS_DESTINATION)' \
		-derivedDataPath $(DERIVED_DATA) test -skip-testing:WristcallPhoneUITests/OnboardingShotsTests $(URL_SETTINGS)

# Screenshots of the iPhone v3 screens (OnboardingShotsTests; own scheme WristcallPhoneUI, never in CI). It
# makes one silent account per run on the Cloud of CLOUD_URL, which it requires (Config/Local.xcconfig does not
# count). Refuses to run without a folder for the PNGs either:
#   CLOUD_URL=http://127.0.0.1:8090 TEST_RUNNER_WRISTCALL_UI_SHOTS=<folder> make shots-ios
# Optional: TEST_RUNNER_WRISTCALL_UI_SERVER + TEST_RUNNER_WRISTCALL_UI_SERVER_TOKEN (agents, new agent) and
# TEST_RUNNER_WRISTCALL_UI_LINK (link confirm). The welcome and the empty agents only show on a simulator without
# the app's account and servers. Nothing is wiped by default. SHOTS_UDID=<udid> runs on that simulator (instead
# of IOS_DESTINATION); SHOTS_RESET=1 (needs SHOTS_UDID) first uninstalls the app and resets the Keychain of THAT
# simulator, which wipes the Keychain of every app on it: use a simulator you do not mind resetting.
SHOTS_UDID ?=
BUNDLE_ID_PREFIX ?= $(shell sed -n 's/^ *BUNDLE_ID_PREFIX *= *//p' Config/Local.xcconfig Config/Base.xcconfig 2>/dev/null | head -1)
PHONE_BUNDLE_ID ?= $(BUNDLE_ID_PREFIX).wristcall
SHOTS_DESTINATION := $(IOS_DESTINATION)
ifneq ($(SHOTS_UDID),)
SHOTS_DESTINATION := platform=iOS Simulator,id=$(SHOTS_UDID)
endif
shots-ios: generate
	$(REQUIRE_CLOUD_URL)
	@test -n "$$TEST_RUNNER_WRISTCALL_UI_SHOTS" || { \
		echo "shots-ios: set TEST_RUNNER_WRISTCALL_UI_SHOTS=<folder> (where the PNGs go)"; exit 1; }
	@if [ -n "$(SHOTS_RESET)" ]; then \
		test -n "$(SHOTS_UDID)" || { echo "shots-ios: SHOTS_RESET=1 needs SHOTS_UDID=<simulator udid>"; exit 1; }; \
		name=$$(xcrun simctl list devices | grep -F "($(SHOTS_UDID))" | sed 's/ *(.*//; s/^ *//'); \
		test -n "$$name" || { echo "shots-ios: no simulator with UDID $(SHOTS_UDID)"; exit 1; }; \
		echo "shots-ios: resetting $$name ($(SHOTS_UDID)): app uninstalled, Keychain of every app wiped"; \
		xcrun simctl boot $(SHOTS_UDID) 2>/dev/null; \
		xcrun simctl list devices | grep -F "($(SHOTS_UDID))" | grep -q Booted || { echo "shots-ios: cannot boot $(SHOTS_UDID)"; exit 1; }; \
		xcrun simctl uninstall $(SHOTS_UDID) $(PHONE_BUNDLE_ID) 2>/dev/null; \
		xcrun simctl keychain $(SHOTS_UDID) reset || exit 1; \
	fi
	xcodebuild -project $(PROJECT) -scheme WristcallPhoneUI -destination '$(SHOTS_DESTINATION)' \
		-derivedDataPath $(DERIVED_DATA) test -only-testing:WristcallPhoneUITests/OnboardingShotsTests $(URL_SETTINGS)

clean:
	rm -rf $(PROJECT) build WristcallKit/.build
