import Foundation

/// The Cloud 0.4.0 account and session routes: silent accounts made from an account key, one session per
/// device, enroll codes for the watch, web sessions and invites. Errors are `APIError` (`APIError.error`).
extension CloudClient {
    /// `POST /v1/accounts` (no token): creates a silent account and its first device.
    public func createAccount(key: AccountKey, device: CloudDeviceInfo) async throws -> SessionTokens {
        try await expect(201, try http.request(
            url("v1/accounts"), method: "POST", json: KeyAndDevice(accountKey: key.value, account: nil, device: device)
        ))
    }

    /// `POST /v1/devices` (no token): signs a device into the account the key belongs to. `account`, if given,
    /// must be that account.
    public func enrollDevice(
        key: AccountKey, account: String? = nil, device: CloudDeviceInfo
    ) async throws -> SessionTokens {
        try await expect(201, try http.request(
            url("v1/devices"), method: "POST",
            json: KeyAndDevice(accountKey: key.value, account: account, device: device)
        ))
    }

    /// `POST /v1/devices` with a fresh Zitadel access token (the migration of a 0.3.x account, or a sign-in):
    /// Cloud finds or creates the account of that identity.
    public func enrollDevice(zitadelAccessToken: String, device: CloudDeviceInfo) async throws -> SessionTokens {
        try await expect(201, try http.request(
            url("v1/devices"), method: "POST", bearer: zitadelAccessToken, json: DeviceOnly(device: device)
        ))
    }

    /// `POST /v1/session/refresh`: a new access token and a new refresh token. The old refresh token is spent:
    /// using it again signs the device out (`APIError.revoked`).
    public func refreshSession(_ refreshToken: String) async throws -> SessionTokens {
        try await expect(200, try http.request(
            url("v1/session/refresh"), method: "POST", json: RefreshBody(refreshToken: refreshToken)
        ))
    }

    /// `PUT /v1/account/key` (app devices): sets the key of an account that has none; the same key again is fine.
    public func setAccountKey(_ key: AccountKey, accessToken: String) async throws {
        try await expectNoContent(try http.request(
            url("v1/account/key"), method: "PUT", bearer: accessToken, json: SetKeyBody(accountKey: key.value)
        ))
    }

    /// `POST /v1/account/key/rotate` with the same key as current and new: signs out every other device, drops
    /// open enroll codes and approved web sessions. Returns how many devices it signed out.
    public func signOutEverywhere(key: AccountKey, accessToken: String) async throws -> Int {
        let body = RotateBody(accountKey: key.value, newAccountKey: key.value)
        let answer: Revoked = try await expect(200, try http.request(
            url("v1/account/key/rotate"), method: "POST", bearer: accessToken, json: body
        ))
        return answer.revoked
    }

    /// `GET /v1/account`.
    public func account(accessToken: String) async throws -> CloudAccountInfo {
        try await expect(200, try http.request(url("v1/account"), method: "GET", bearer: accessToken))
    }

    /// `GET /v1/devices`.
    public func devices(accessToken: String) async throws -> [AccountDevice] {
        let list: DeviceList = try await expect(200, try http.request(url("v1/devices"), method: "GET", bearer: accessToken))
        return list.devices
    }

    /// `DELETE /v1/devices/{id}`: any device may remove itself, only a manager (app or web) another one.
    public func revokeDevice(id: String, accessToken: String) async throws {
        try await expectNoContent(try http.request(
            url("v1/devices").appending(component: id), method: "DELETE", bearer: accessToken
        ))
    }

    /// `POST /v1/devices/enroll-codes` (app devices): a single use code that signs in one watch.
    public func enrollCode(accessToken: String) async throws -> EnrollCode {
        try await expect(201, try http.request(url("v1/devices/enroll-codes"), method: "POST", bearer: accessToken))
    }

    /// `POST /v1/web-sessions/{id}/inspect` (app devices): what the browser asking for the session looks like.
    /// `key` is the approve key from the QR code.
    public func inspectWebSession(id: String, key: String, accessToken: String) async throws -> WebSessionInfo {
        try await expect(200, try http.request(
            webSession(id, "inspect"), method: "POST", bearer: accessToken, json: ApproveBody(approveKey: key)
        ))
    }

    /// `POST /v1/web-sessions/{id}/approve` (app devices).
    public func approveWebSession(id: String, key: String, accessToken: String) async throws {
        let request = try http.request(
            webSession(id, "approve"), method: "POST", bearer: accessToken, json: ApproveBody(approveKey: key)
        )
        let (status, data, headers) = try await http.send(request)
        guard status == 200 else { throw APIHTTP.error(status: status, data: data, headers: headers) }
    }

    /// `POST /v1/entitlements/redeem` (app or web): redeems an invite code (`WC-XXXX-XXXX-XXXX`). A wrong or
    /// spent code is `APIError.invalidCode`; an account that already has a plan gets `.conflict` and the invite
    /// is not spent.
    public func redeemInvite(_ code: String, accessToken: String) async throws -> Plan {
        try await expect(200, try http.request(
            url("v1/entitlements/redeem"), method: "POST", bearer: accessToken, json: CodeBody(code: code)
        ))
    }

    /// One path segment for the id (`/` is percent-encoded), then the action.
    private func webSession(_ id: String, _ action: String) -> URL {
        url("v1/web-sessions").appending(component: id).appending(path: action)
    }
}

private struct KeyAndDevice: Encodable {
    var accountKey: String
    var account: String?
    var device: CloudDeviceInfo

    private enum CodingKeys: String, CodingKey {
        case account, device
        case accountKey = "account_key"
    }
}

private struct DeviceOnly: Encodable { var device: CloudDeviceInfo }

private struct RefreshBody: Encodable {
    var refreshToken: String
    private enum CodingKeys: String, CodingKey { case refreshToken = "refresh_token" }
}

private struct SetKeyBody: Encodable {
    var accountKey: String
    private enum CodingKeys: String, CodingKey { case accountKey = "account_key" }
}

private struct RotateBody: Encodable {
    var accountKey: String
    var newAccountKey: String
    private enum CodingKeys: String, CodingKey {
        case accountKey = "account_key"
        case newAccountKey = "new_account_key"
    }
}

private struct ApproveBody: Encodable {
    var approveKey: String
    private enum CodingKeys: String, CodingKey { case approveKey = "approve_key" }
}

private struct CodeBody: Encodable { var code: String }
private struct Revoked: Decodable { var revoked: Int }
private struct DeviceList: Decodable { var devices: [AccountDevice] }
