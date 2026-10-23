import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

struct NodeAccountTests {
    static func usage(used: Int, quota: Int = 36_000) -> CloudUsage {
        CloudUsage(month: "2026-10", usedS: used, quotaS: quota, remainingS: max(0, quota - used))
    }

    @Test func usageLevels() {
        let normal = Self.usage(used: 479 * 60)
        #expect(normal.level == .normal)
        #expect(normal.warning == nil)

        let warning = Self.usage(used: 480 * 60)
        #expect(warning.level == .warning(percent: 80))
        #expect(warning.warning == "You've used 80% of this month's call minutes.")
        #expect(Self.usage(used: 599 * 60 + 59).level == .warning(percent: 99))

        let exhausted = Self.usage(used: 600 * 60)
        #expect(exhausted.level == .exhausted)
        #expect(exhausted.warning == "You've used this month's call minutes. They renew next month.")
        #expect(Self.usage(used: 700 * 60).level == .exhausted)
        // A zero quota has nothing left to use.
        #expect(Self.usage(used: 0, quota: 0).level == .exhausted)
    }

    @Test func usageSummaryRoundsDown() {
        let usage = Self.usage(used: 125)
        #expect(usage.usedMinutes == 2)
        #expect(usage.quotaMinutes == 600)
        #expect(usage.summary == "2 of 600 min used this month")
        #expect(Self.usage(used: 59, quota: 119).summary == "0 of 1 min used this month")
    }

    @Test func nodeMeDecodesPlanAndUsage() async throws {
        let withUsage = #"""
        {"device_id":"dev_1","device_name":"watchos","user":{"id":"acc_1","handle":"acc_1","display_name":""},
         "agents":[],"profiles":[],
         "plan":{"plan":"full","source":"beta","expires_at":null},
         "usage":{"month":"2026-10","used_s":125,"quota_s":36000,"remaining_s":35875}}
        """#
        let withoutUsage = #"{"device_id":"dev_1","agents":[],"plan":null}"#
        let host = StubHost(replies: [(200, withUsage), (200, withoutUsage)])
        let client = ManagementClient(server: host.url, token: "eyJ.node", session: .stubbed())

        let account = try await client.me()
        #expect(account.plan == Plan(plan: .full, source: "beta"))
        #expect(account.usage == CloudUsage(month: "2026-10", usedS: 125, quotaS: 36_000, remainingS: 35_875))
        let request = try #require(host.requests.first)
        #expect(request.method == "GET")
        #expect(request.path == "/v1/me")
        #expect(request.headers["Authorization"] == "Bearer eyJ.node")

        let bare = try await client.me()
        #expect(bare.plan == nil)
        #expect(bare.usage == nil)
    }

    @Test func providerListReadsCloudFlags() throws {
        let cloud = try JSONDecoder().decode(ProviderList.self, from: Data(#"""
        {"providers":[],"custom_endpoints":false,"public_only":true,"history_only_one_way":true}
        """#.utf8))
        #expect(cloud.publicOnly)
        #expect(cloud.historyOnlyOneWay)

        let selfHosted = try JSONDecoder().decode(ProviderList.self, from: Data(#"{"providers":[],"custom_endpoints":true}"#.utf8))
        #expect(!selfHosted.publicOnly)
        #expect(!selfHosted.historyOnlyOneWay)
        #expect(selfHosted.customEndpoints)
    }

    @Test func callRecordSavedToHistory() throws {
        func record(_ status: String, delivery: String?) throws -> CallRecord {
            let field = delivery.map { #","delivery":"\#($0)""# } ?? ""
            let json = #"{"id":"c_1","call_type":"one-shot","status":"\#(status)","attempts":0,"created_at":1,"entries":[]\#(field)}"#
            return try JSONDecoder().decode(CallRecord.self, from: Data(json.utf8))
        }
        let saved = try record("delivered", delivery: "history")
        #expect(saved.delivery == "history")
        #expect(saved.savedToHistory)
        #expect(try !record("delivered", delivery: "webhook").savedToHistory)
        #expect(try !record("processing", delivery: "history").savedToHistory)
        let older = try record("delivered", delivery: nil)
        #expect(older.delivery == nil)
        #expect(!older.savedToHistory)
    }

    @Test func agentDetailFromCloudAgent() {
        let agent = CloudAgent(id: "ag_1", slug: "notes", displayName: "Notes", icon: "mic", callType: "one-shot",
                               color: .blue)
        let detail = AgentDetail(cloud: agent)
        #expect(detail.id == "ag_1")
        #expect(detail.slug == "notes")
        #expect(detail.displayName == "Notes")
        #expect(detail.icon == "mic")
        #expect(detail.callType == "one-shot")
        #expect(detail.isOneWay)
        #expect(detail.color == agent.color)
        #expect(detail.action == nil)
        #expect(detail.stt == nil)
    }
}
