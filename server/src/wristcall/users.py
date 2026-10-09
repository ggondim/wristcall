"""Users of a server and their personal API tokens."""

import secrets
import time
from collections.abc import Callable

from .agents import SLUG
from .pairing import hash_secret
from .storage import ApiToken, Conflict, Storage, User

API_TOKEN_PREFIX = "wc_pat_"


class UserError(Exception):
    pass


def new_user_id() -> str:
    return f"u_{secrets.token_hex(6)}"


class UserService:
    def __init__(self, storage: Storage, *, now: Callable[[], float] = time.time) -> None:
        self._st = storage
        self._now = now

    @staticmethod
    def _check_handle(handle: str) -> None:
        if not SLUG.fullmatch(handle):
            raise UserError("handle: use 1 to 32 lowercase letters, digits or hyphens, starting with a letter or digit")

    async def create(self, handle: str, display_name: str | None = None) -> User:
        self._check_handle(handle)
        try:
            return await self._st.users.create(new_user_id(), handle, (display_name or handle).strip()[:64], self._now())
        except Conflict:
            raise UserError(f"user already exists: {handle}") from None

    async def resolve(self, handle: str | None) -> User:
        """The user with this handle; without a handle, the only user (a single-user server needs no --user)."""
        if handle is not None:
            user = await self._st.users.by_handle(handle)
            if user is None:
                raise UserError(f"user not found: {handle}")
            return user
        users = await self._st.users.list()
        if not users:
            raise UserError("there are no users yet; create one with: wristcall users add <handle>")
        if len(users) > 1:
            raise UserError("this server has several users; choose one with --user <handle>")
        return users[0]

    async def rename(self, handle: str, new_handle: str | None = None, display_name: str | None = None) -> User:
        user = await self.resolve(handle)
        if new_handle is not None:
            self._check_handle(new_handle)
        if display_name is not None:
            display_name = display_name.strip()[:64]
            if not display_name:
                raise UserError("display name cannot be empty")
        try:
            updated = await self._st.users.update(user.id, handle=new_handle, display_name=display_name)
        except Conflict:
            raise UserError(f"user already exists: {new_handle}") from None
        assert updated is not None
        return updated

    async def delete(self, handle: str) -> User:
        user = await self.resolve(handle)
        await self._st.users.delete(user.id)
        return user

    async def issue_token(self, user_id: str, name: str) -> tuple[ApiToken, str]:
        """Returns the record and the token itself, which is shown once and never stored."""
        token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = await self._st.tokens.create(f"t_{secrets.token_hex(6)}", user_id, name.strip()[:64] or "token", hash_secret(token), self._now())
        return record, token

    async def list_tokens(self, user_id: str) -> list[ApiToken]:
        return await self._st.tokens.list(user_id)

    async def revoke_token(self, token_id: str) -> bool:
        return await self._st.tokens.revoke(token_id, self._now())
