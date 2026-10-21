"""`GET /v1/node/me`: who the node is serving and what they may do."""

import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from .config import CloudConfig
from .node_auth import NodeCaller, node_caller
from .node_core import list_agents, month_usage
from .sessions_api import entitlement_response

router = APIRouter(prefix="/v1/node")


@router.get("/me")
async def me(request: Request, caller: NodeCaller = Depends(node_caller)) -> dict[str, Any]:
    cloud = request.app.state.cloud
    config: CloudConfig = request.app.state.config
    now = time.time()
    entitlement = await request.app.state.accounts.entitlement(caller.account, now=now)
    return {
        "account": caller.account,
        "plan": entitlement_response(entitlement) if entitlement is not None else None,
        "device": {"id": caller.device, "platform": caller.platform},
        "agents": await list_agents(cloud, caller.account),
        "usage": await month_usage(cloud, config, caller.account, now),
    }
