"""Links a local user to the central account (OIDC, decision 17).

The central account's access token is never a management credential: it only proves who the person is in
the central account. Linking needs a second, local proof (a user API token or a pairing code of this server),
so a token issued for another server cannot take over a user here.
"""

import logging
import time
from collections.abc import Callable

import httpx

from .config import CentralAccountConfig
from .oidc import Identity, OidcError, OidcUnavailable, OidcVerifier, normalize_issuer
from .pairing import normalize_code
from .storage import Conflict, Storage, User
from .users import UserService

log = logging.getLogger("wristcall.account")

APP_TOKEN_NAME = "account link"


class AccountError(Exception):
    """codes: invalid_account_token, account_unavailable, conflict, not_found, invalid_code. Never carries a token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def account_key(issuer: str, subject: str) -> str:
    """The user's central account here: `"<Cloud URL>#<sub>"`. The Cloud's `sub` is already
    `"<account issuer>#<account sub>"`, so the key reads `"<Cloud URL>#<account issuer>#<account sub>"`. Links made
    while the issuer was the account issuer itself (0.5.0) no longer match, and changing the Cloud URL undoes them."""
    return f"{normalize_issuer(issuer)}#{subject}"


class AccountService:
    def __init__(
        self,
        storage: Storage,
        config: CentralAccountConfig,
        http: httpx.AsyncClient,
        *,
        verifier: OidcVerifier | None = None,
        now: Callable[[], float] = time.time,
        max_attempts: int = 5,
    ) -> None:
        self.config = config
        self._st = storage
        self._verifier = verifier or OidcVerifier(
            config.issuer, config.audience, http, clients=config.clients, typ="wc-server+jwt"
        )
        self._now = now
        self._max_attempts = max_attempts

    async def identify(self, token: str) -> Identity:
        try:
            return await self._verifier.verify(token)
        except OidcError as e:
            # OidcError messages describe the failure, never the token.
            raise AccountError("invalid_account_token", f"central account token rejected: {e}") from None
        except OidcUnavailable as e:
            log.warning("central account issuer unavailable: %s", e)
            raise AccountError("account_unavailable", "the central account is unavailable; try again later") from None

    async def user_for(self, token: str) -> User | None:
        identity = await self.identify(token)
        return await self._st.users.by_central(account_key(identity.issuer, identity.subject))

    async def link(self, user_id: str, token: str) -> str:
        identity = await self.identify(token)
        return await self._link(user_id, identity)

    async def link_with_code(self, code: str, token: str) -> tuple[str, str]:
        # The token is checked first: a bad token must not spend (or burn) the code.
        identity = await self.identify(token)
        now = self._now()
        normalized = normalize_code(code) if isinstance(code, str) else None
        user_id = await self._st.pairing.claim_code(normalized, now, self._max_attempts) if normalized else None
        if user_id is None:
            await self._st.pairing.count_failed_attempt(now)
            raise AccountError("invalid_code", "invalid or expired code")
        return user_id, await self._link(user_id, identity)

    async def unlink(self, user_id: str) -> bool:
        return await self._st.users.unlink_central(user_id)

    async def issue_app_token(self, user_id: str) -> tuple[User, str]:
        """A new API token for the app that linked with a code, so it can manage this server from now on."""
        user = await self._st.users.get(user_id)
        if user is None:
            raise AccountError("not_found", "user not found")
        _record, token = await UserService(self._st, now=self._now).issue_token(user_id, APP_TOKEN_NAME)
        return user, token

    async def _link(self, user_id: str, identity: Identity) -> str:
        key = account_key(identity.issuer, identity.subject)
        try:
            linked = await self._st.users.link_central(user_id, key)
        except Conflict:
            raise AccountError("conflict", "this central account is already linked to another user") from None
        if not linked:
            raise AccountError("not_found", "user not found")
        return key
