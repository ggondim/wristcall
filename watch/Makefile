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

# Integration tests (see README.md): a local wristcall server with fake providers.
TEST_SERVER_PORT ?= 8765
export WRISTCALL_TEST_CONFIG ?= $(CURDIR)/WristcallKit/Tests/test-server.yaml
export WRISTCALL_TEST_DATA_DIR ?= $(CURDIR)/build/test-server-data
export WRISTCALL_TEST_CLI ?= wristcall

.PHONY: generate test-kit test-server test-integration build test-sim clean

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

build: generate
	$(XCODEBUILD) build

test-sim: generate
	$(XCODEBUILD) test

clean:
	rm -rf $(PROJECT) build WristcallKit/.build
