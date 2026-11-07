from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from cairn.server.tokens import verify_token

DISPATCHER_USERNAME = "dispatcher"

_BASIC_REALM = "cairn"

Role = Literal["user", "dispatcher"]
ROLE_USER: Role = "user"
ROLE_DISPATCHER: Role = "dispatcher"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Credentials for the two authenticated principals of the Cairn API.

    Regular users authenticate with ``username``/``password``. Dispatchers
    boot up unauthenticated and exchange the fixed username ``dispatcher``
    plus ``dispatcher_api_key`` for a JWT at ``POST /runtime/dispatchers/login``;
    every later request is authorized with that JWT as a bearer token.
    """

    username: str
    password: str
    dispatcher_api_key: str

    def __post_init__(self) -> None:
        if self.username == DISPATCHER_USERNAME:
            raise ValueError(f"auth username must not be the reserved name {DISPATCHER_USERNAME!r}")
        if self.password == self.dispatcher_api_key:
            raise ValueError("auth password and dispatcher api key must not be the same value")
        if ":" in self.username:
            raise ValueError("auth username must not contain ':' (HTTP Basic splits on the first colon)")
        for name, value in (
            ("username", self.username),
            ("password", self.password),
            ("dispatcher_api_key", self.dispatcher_api_key),
        ):
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(f"auth {name} must only contain ASCII characters") from exc


@dataclass(frozen=True, slots=True)
class Principal:
    role: Role
    dispatcher_id: str | None = None


_basic_scheme = HTTPBasic(realm=_BASIC_REALM, auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "invalid credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'},
    )


def authenticate(
    request: Request,
    basic: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> Principal:
    if bearer is not None:
        secret: bytes = request.app.state.dispatcher_jwt_secret
        dispatcher_id = verify_token(secret, bearer.credentials)
        if dispatcher_id is None or request.app.state.dispatcher_registry.get(dispatcher_id) is None:
            raise _unauthorized()
        return Principal(role=ROLE_DISPATCHER, dispatcher_id=dispatcher_id)
    if basic is not None:
        config: AuthConfig = request.app.state.auth_config
        if basic.username == config.username and basic.password == config.password:
            return Principal(role=ROLE_USER)
        raise _unauthorized()
    raise _unauthorized("Not authenticated")


def authenticate_dispatcher_login(
    request: Request,
    basic: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
) -> None:
    """Allow only the fixed dispatcher Basic credentials, used by the
    ``POST /runtime/dispatchers/login`` endpoint."""
    config: AuthConfig = request.app.state.auth_config
    if basic is None:
        raise _unauthorized("Not authenticated")
    if basic.username != DISPATCHER_USERNAME or basic.password != config.dispatcher_api_key:
        raise _unauthorized()


def require_user(principal: Annotated[Principal, Depends(authenticate)]) -> None:
    if principal.role != ROLE_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires user credentials",
        )


def require_dispatcher(principal: Annotated[Principal, Depends(authenticate)]) -> str:
    if principal.role != ROLE_DISPATCHER or principal.dispatcher_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires a dispatcher bearer token",
        )
    return principal.dispatcher_id
