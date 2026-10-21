"""Push for the cloud-mode nodes: where a device's notifications go (`PUT`/`DELETE /v1/node/push-targets`), the node's
own `POST /v1/node/push` and the Cloud's 80% quota warning (`send_quota_warning`).

A push target is the push registration a device made, kept under `(account, device)` as the registration's id, never
its key: sending needs no secret. The node's push reaches only the target of the device it acts for (the proof is the
call grant or the user token); the quota warning reaches every target of the account. A target whose device or
registration is gone is deleted when found. Errors never echo the key, the token or the message.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.datastructures import State

from .api_common import ApiError, json_object
from .node_auth import NodeCaller, current_node, node_caller, verify_grant
from .push.api import relay_to_registration
from .push.registry import PushError, is_push_key, key_id

log = logging.getLogger("wristcall_cloud.node_push")

router = APIRouter(prefix="/v1/node")

QUOTA_WARNING_TITLE = "wristcall Cloud"
QUOTA_WARNING_BODY = "You have used 80% of this month's minutes."


def _invalid(message: str) -> ApiError:
    return ApiError("invalid", message, 422)


async def _send_to_target(state: State, target: dict[str, Any], body: dict[str, Any]) -> str:
    """Relays `body` to the target's registration: the outcome of `relay_to_registration`, or `"gone"` when the
    device or the registration no longer exists (the target is deleted: only if it still names that registration)."""
    gone = {"_id": target["_id"], "registration": target["registration"]}
    if await state.accounts.device(target["account"], target["device"]) is None:
        await state.cloud.push_targets.delete_one(gone)
        return "gone"
    registration = await state.store.get_registration(target["registration"])
    if registration is None:
        await state.cloud.push_targets.delete_one(gone)
        return "gone"
    outcome = await relay_to_registration(state, target["registration"], registration, body)
    if outcome == "gone":
        await state.cloud.push_targets.delete_one(gone)
    return outcome


@router.put("/push-targets", status_code=204)
async def put_push_target(request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    body = await json_object(request)
    push_key = body.get("push_key")
    if set(body) != {"push_key"} or not isinstance(push_key, str) or not is_push_key(push_key):
        raise _invalid("body must be an object with a push_key")
    registration = key_id(push_key)
    if await request.app.state.store.get_registration(registration) is None:
        raise _invalid("push_key is not registered")
    target_id = f"{caller.account}:{caller.device}"
    await request.app.state.cloud.push_targets.replace_one(
        {"_id": target_id},
        {
            "_id": target_id, "account": caller.account, "device": caller.device, "platform": caller.platform,
            "registration": registration, "updated_at": time.time(),
        },
        upsert=True,
    )
    return Response(status_code=204)


@router.delete("/push-targets", status_code=204)
async def delete_push_target(request: Request, caller: NodeCaller = Depends(node_caller)) -> Response:
    await request.app.state.cloud.push_targets.delete_one({"_id": f"{caller.account}:{caller.device}"})
    return Response(status_code=204)


@router.post("/push", status_code=202)
async def push(
    request: Request,
    node: dict[str, Any] = Depends(current_node),
    x_wristcall_user_token: str | None = Header(default=None),
    x_wristcall_call_grant: str | None = Header(default=None),
) -> dict[str, int]:
    """A notification for the device the node acts for: proven by the grant of a call (named in `call`; an expired
    grant does not do) or, without one, by the user token. `sent` is 0 when the device has no target, does not take
    the event, or the push could not go out (the reason is in the log)."""
    body = await json_object(request)
    call = body.pop("call", None)  # not part of the message: `parse_message` refuses fields it does not know
    if x_wristcall_call_grant is not None:
        if not isinstance(call, str) or not call:
            raise _invalid("call must be the id of the call")
        grant = verify_grant(request, node, call, x_wristcall_call_grant, allow_expired=False)
        account, device = grant.account, grant.device
    else:
        caller = await node_caller(request, node, x_wristcall_user_token)
        account, device = caller.account, caller.device
    target = await request.app.state.cloud.push_targets.find_one({"_id": f"{account}:{device}"})
    if target is None:
        return {"sent": 0}
    try:
        outcome = await _send_to_target(request.app.state, target, body)
    except PushError as e:
        raise _invalid(e.message) from None
    if outcome not in ("sent", "skipped", "gone"):
        log.warning("node push not sent (%s)", outcome)
    return {"sent": 1 if outcome == "sent" else 0}


async def send_quota_warning(state: State, account: str, usage: dict[str, Any]) -> int:
    """Tells every device of `account` with a push target that 80% of the month's quota is used (`usage` has `month`,
    `used_s` and `quota_s`); how many were sent. A failure goes to the log and never stops the others."""
    body = {
        "event": "quota.warning",
        "title": QUOTA_WARNING_TITLE,
        "body": QUOTA_WARNING_BODY,
        "data": {"month": usage["month"], "used_s": usage["used_s"], "quota_s": usage["quota_s"]},
    }
    try:
        targets = await state.cloud.push_targets.find({"account": account}).to_list(None)
    except Exception as e:  # the warning is best effort: the call is already closed
        log.warning("quota warning not sent, targets unreadable (%s)", type(e).__name__)
        return 0
    sent = 0
    for target in targets:
        try:
            outcome = await _send_to_target(state, target, body)
        except Exception as e:
            log.warning("quota warning not sent to a device (%s)", type(e).__name__)
            continue
        if outcome == "sent":
            sent += 1
        elif outcome != "skipped":
            log.info("quota warning not sent to a device (%s)", outcome)
    return sent
