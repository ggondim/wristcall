"""The account proof of the request being served in cloud mode.

The Cloud acts for a user only with that user's per-server token, but the services above the storage
(`AgentService`, `History`) pass only a `user_id`. The authenticator records the verified token here, in a
`ContextVar` (one value per request or WebSocket task), and the Cloud storage adapter reads it back, checking that it
is the same account the service asked for.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserProof:
    account: str
    device: str
    platform: str
    token: str = field(repr=False)  # the user's per-server token, sent to the Cloud as is; never logged


class ProofMismatch(PermissionError):
    """No proof for this request, or one for another account than the service asked for."""


current_proof: ContextVar[UserProof | None] = ContextVar("wristcall_cloud_proof", default=None)


def require_proof(user_id: str) -> UserProof:
    """The proof of the current request, which must be for `user_id`."""
    proof = current_proof.get()
    if proof is None:
        raise ProofMismatch("no account proof for this request")
    if proof.account != user_id:
        raise ProofMismatch("the account proof is for another account")
    return proof
