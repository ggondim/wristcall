import Foundation

/// What a cloud-mode node's `GET /v1/me` adds for the apps: the account's plan and its call minutes this month.
public struct NodeAccount: Decodable, Sendable, Equatable {
    /// `nil` without a plan.
    public var plan: Plan?
    /// `nil` when the node sent none or one this build cannot read (the app says usage is unavailable).
    public var usage: CloudUsage?

    public init(plan: Plan? = nil, usage: CloudUsage? = nil) {
        self.plan = plan
        self.usage = usage
    }

    private enum CodingKeys: String, CodingKey {
        case plan, usage
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        plan = try container.decodeIfPresent(Plan.self, forKey: .plan)
        usage = (try? container.decodeIfPresent(CloudUsage.self, forKey: .usage)) ?? nil
    }
}

/// The account's call minutes in the month (`usage` of `GET /v1/me`). Times are seconds.
public struct CloudUsage: Decodable, Sendable, Equatable {
    /// `YYYY-MM`.
    public var month: String
    public var usedS: Int
    public var quotaS: Int
    public var remainingS: Int

    public init(month: String, usedS: Int, quotaS: Int, remainingS: Int) {
        self.month = month
        self.usedS = usedS
        self.quotaS = quotaS
        self.remainingS = remainingS
    }

    private enum CodingKeys: String, CodingKey {
        case month
        case usedS = "used_s"
        case quotaS = "quota_s"
        case remainingS = "remaining_s"
    }

    /// Whole minutes, rounded down.
    public var usedMinutes: Int { usedS / 60 }
    public var quotaMinutes: Int { quotaS / 60 }

    /// Exhausted once the quota is used up (a quota of zero or less counts as used up); a warning from 80 %.
    public var level: UsageLevel {
        if quotaS <= 0 || usedS >= quotaS { return .exhausted }
        if usedS * 5 >= quotaS * 4 { return .warning(percent: usedS * 100 / quotaS) }
        return .normal
    }

    /// "12 of 600 min used this month".
    public var summary: String {
        "\(usedMinutes) of \(quotaMinutes) min used this month"
    }

    /// What the app shows next to the summary when the minutes are running out; `nil` below 80 %.
    public var warning: String? {
        switch level {
        case .normal: nil
        case .warning(let percent): "You've used \(percent)% of this month's call minutes."
        case .exhausted: "You've used this month's call minutes. They renew next month."
        }
    }
}

public enum UsageLevel: Sendable, Equatable {
    case normal
    /// From 80 % of the quota (`percent` rounded down), until it is used up.
    case warning(percent: Int)
    case exhausted
}
