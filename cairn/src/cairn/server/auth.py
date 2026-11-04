from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

DISPATCHER_USERNAME = "dispatcher"

_BASIC_REALM = "cairn"

Role = Literal["user", "dispatcher"]
ROLE_USER: Role = "user"
ROLE_DISPATCHER: Role = "dispatcher"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Credentials for the two authenticated principals of the Cairn API.

    Regular users authenticate with ``username``/``password``; dispatchers
    authenticate with the fixed username ``dispatcher`` and
    ``dispatcher_api_key`` as the password.
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
        # HTTP Basic headers are ASCII-decoded on the server (fastapi) and
        # latin-1-encoded on the client (requests), so credentials outside
        # ASCII can never match — reject them at startup instead.
        for name, value in (
            ("username", self.username),
            ("password", self.password),
            ("dispatcher_api_key", self.dispatcher_api_key),
        ):
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(f"auth {name} must only contain ASCII characters") from exc


_basic_scheme = HTTPBasic(realm=_BASIC_REALM)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credentials",
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'},
    )


def authenticate(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(_basic_scheme)],
) -> Role:
    # malformed/missing Authorization headers are rejected by _basic_scheme
    # itself (401 with the same WWW-Authenticate realm); reaching this point
    # means the payload parsed into valid credentials
    config: AuthConfig = request.app.state.auth_config
    if credentials.username == DISPATCHER_USERNAME:
        if credentials.password != config.dispatcher_api_key:
            raise _unauthorized()
        return ROLE_DISPATCHER
    if credentials.username == config.username and credentials.password == config.password:
        return ROLE_USER
    raise _unauthorized()


def require_user(role: Annotated[Role, Depends(authenticate)]) -> None:
    if role != ROLE_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires user credentials",
        )
