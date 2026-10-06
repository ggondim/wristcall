# wristcall watch app: generate, build and test. Run from watch/ or with `make -C watch <target>`.

PROJECT := Wristcall.xcodeproj
SCHEME := Wristcall
# Any xcodebuild destination. The default is the maintainer's "WC Series 7" simulator
# (Apple Watch Series 7 45mm, watchOS 26.5); list yours with `xcrun simctl list devices watchOS`.
DESTINATION ?= platform=watchOS Simulator,id=17F60D6F-5995-4C9D-8FF8-36F7367860A3
DERIVED_DATA := build/DerivedData
XCODEBUILD := xcodebuild -project $(PROJECT) -scheme $(SCHEME) -destination '$(DESTINATION)' \
	-derivedDataPath $(DERIVED_DATA) CODE_SIGNING_ALLOWED=NO

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
		--config $(WRISTCALL_TEST_CONFIG) --host 127.0.0.1 --port $(TEST_SERVER_PORT)

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
