import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

/// Create, edit and delete against a running test server (`make test-server`). Skipped unless
/// `TEST_RUNNER_WRISTCALL_TEST_SERVER` and `TEST_RUNNER_WRISTCALL_TEST_TOKEN` (a `wc_pat_` token) reach
/// the test process.
private let liveConfig: (URL, String)? = {
    let env = ProcessInfo.processInfo.environment
    guard let text = env["WRISTCALL_TEST_SERVER"], let url = URL(string: text),
          let token = env["WRISTCALL_TEST_TOKEN"], !token.isEmpty else { return nil }
    return (url, token)
}()

@MainActor
@Suite(.enabled(if: liveConfig != nil, "set WRISTCALL_TEST_SERVER and WRISTCALL_TEST_TOKEN"))
struct AgentsLiveTests {
    @Test func oneShotWebhookAgentLifecycle() async throws {
        let (url, token) = try #require(liveConfig)
        let server = ManagedServer(name: "Test", url: url, token: token)
        let model = AgentsModel(server: server, api: LiveServerAPI(server: url, token: token))
        await model.load()
        #expect(model.error == nil)
        let providers = try #require(model.providers)
        #expect(providers.customEndpoints)

        // Create a one-shot agent with its own webhook.
        let create = AgentFormModel(editing: nil, providers: providers)
        create.displayName = "Smoke Hook"
        create.slug = AgentFormModel.suggestSlug(from: create.displayName)
        create.callType = "one-shot"
        create.stt = .provider("demo-stt")
        create.action = .custom
        create.webhookURL = "https://example.com/hook"
        create.webhookHeaderName = "X-Key"
        create.webhookHeaderValue = "s3cret"
        #expect(create.validationMessage == nil)
        #expect(await model.save(create.fields(), editing: nil) == nil)
        let created = try #require(model.agents.first { $0.slug == "smoke-hook" })
        #expect(created.callType == "one-shot")
        #expect(created.tts == nil || created.tts == .null)
        #expect(created.action?["url"] == .string("https://example.com/hook"))
        #expect(created.action?["headers"]?["X-Key"] == .string("***"))

        // Edit only the name: the masked header is not touched.
        let edit = AgentFormModel(editing: created, providers: providers)
        edit.displayName = "Smoke Hook 2"
        #expect(edit.fields() == ["display_name": .string("Smoke Hook 2")])
        #expect(await model.save(edit.fields(), editing: created) == nil)
        let renamed = try #require(model.agents.first { $0.slug == "smoke-hook" })
        #expect(renamed.displayName == "Smoke Hook 2")
        #expect(renamed.action?["headers"]?["X-Key"] == .string("***"))

        // Changing the webhook address keeps the stored secret.
        let again = AgentFormModel(editing: renamed, providers: providers)
        again.webhookURL = "https://example.com/hook2"
        #expect(await model.save(again.fields(), editing: renamed) == nil)
        let moved = try #require(model.agents.first { $0.slug == "smoke-hook" })
        #expect(moved.action?["url"] == .string("https://example.com/hook2"))
        #expect(moved.action?["headers"]?["X-Key"] == .string("***"))

        // The same slug twice is refused with the server's text.
        let duplicate = AgentFormModel(editing: nil, providers: providers)
        duplicate.displayName = "Smoke Hook"
        duplicate.slug = "smoke-hook"
        duplicate.callType = "one-shot"
        duplicate.stt = .provider("demo-stt")
        duplicate.action = .custom
        duplicate.webhookURL = "https://example.com/hook"
        let refused = await model.save(duplicate.fields(), editing: nil)
        #expect(refused?.contains("already exists") == true)

        // Move it to the top, then delete it.
        await model.move(from: IndexSet(integer: model.agents.count - 1), to: 0)
        #expect(model.agents.first?.slug == "smoke-hook")
        await model.delete(try #require(model.agents.first))
        #expect(!model.agents.contains { $0.slug == "smoke-hook" })
        #expect(model.error == nil)
    }
}
