import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct UsageModelTests {
    private let entry = CloudServer(
        id: "cs-9", name: "Wristcall Cloud", url: "https://cloud.wristcall.test", kind: "cloud", linked: true, agents: [])

    private func state(withEntry: Bool = true, cloud: FakeServerAPI) -> AppState {
        let state = AppState(store: InMemoryManagedServerStore([]), makeAPI: { _, _ in FakeServerAPI() })
        _ = state.loadStoreIfNeeded()
        if withEntry {
            state.setCloudEntry(entry)
            state.cloudAPI = cloud
        }
        return state
    }

    @Test func usageLoadsFromCloudEntry() async {
        let cloud = FakeServerAPI()
        cloud.meResult = .success(NodeAccount(usage: CloudUsage(month: "2026-10", usedS: 28_800, quotaS: 36_000, remainingS: 7_200)))
        let model = UsageModel(state: state(cloud: cloud))
        await model.load()
        #expect(model.usage?.summary == "480 of 600 min used this month")
        #expect(model.usage?.warning == "You've used 80% of this month's call minutes.")
        #expect(!model.unavailable)
    }

    @Test func usageUnavailableWithoutNode() async {
        let cloud = FakeServerAPI()
        cloud.meResult = .failure(APIError.noNodeAvailable(retryAfter: nil))
        let model = UsageModel(state: state(cloud: cloud))
        await model.load()
        #expect(model.unavailable)
        #expect(model.usage == nil)
    }

    @Test func failedReloadKeepsTheLastUsage() async {
        let cloud = FakeServerAPI()
        cloud.meResult = .success(NodeAccount(usage: CloudUsage(month: "2026-10", usedS: 600, quotaS: 36_000, remainingS: 35_400)))
        let model = UsageModel(state: state(cloud: cloud))
        await model.load()
        cloud.meResult = .failure(APIError.noNodeAvailable(retryAfter: nil))
        await model.load()
        #expect(model.unavailable)
        #expect(model.usage?.summary == "10 of 600 min used this month")
        // It comes back with the next answer.
        cloud.meResult = .success(NodeAccount(usage: CloudUsage(month: "2026-10", usedS: 1_200, quotaS: 36_000, remainingS: 34_800)))
        await model.load()
        #expect(!model.unavailable)
        #expect(model.usage?.summary == "20 of 600 min used this month")
    }

    @Test func accountWithoutReadableUsageIsUnavailable() async {
        let cloud = FakeServerAPI()
        cloud.meResult = .success(NodeAccount(plan: nil, usage: nil))
        let model = UsageModel(state: state(cloud: cloud))
        await model.load()
        #expect(model.unavailable)
        #expect(model.usage == nil)
    }

    @Test func noCloudEntryMeansNoUsageRequest() async {
        let cloud = FakeServerAPI()
        let model = UsageModel(state: state(withEntry: false, cloud: cloud))
        await model.load()
        #expect(cloud.calls.isEmpty)
        #expect(model.usage == nil)
        #expect(!model.unavailable)
    }
}
