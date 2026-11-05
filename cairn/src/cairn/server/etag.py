from __future__ import annotations

import functools
import hashlib
import re
from collections.abc import Callable
from typing import Concatenate

from fastapi import Request, Response
from pydantic import BaseModel

_ETAG_ELEMENT = re.compile(r'W/"[^"]*"|"[^"]*"')


def _serialize(data: BaseModel | list[BaseModel]) -> bytes:
    if isinstance(data, list):
        return b"[" + b",".join(item.model_dump_json(by_alias=True).encode("utf-8") for item in data) + b"]"
    return data.model_dump_json(by_alias=True).encode("utf-8")


def _matches_if_none_match(header: str, etag: str) -> bool:
    if header.strip() == "*":
        return True
    for candidate in _ETAG_ELEMENT.findall(header):
        if candidate.removeprefix("W/") == etag:
            return True
    return False


def etag[**P, M: BaseModel](func: Callable[Concatenate[Request, P], M | list[M]]) -> Callable[Concatenate[Request, P], Response]:
    """The endpoint body (including expiry side effects) always runs; on a
    matching If-None-Match only the response transfer is skipped.
    """

    @functools.wraps(func)
    def wrapper(request: Request, *args: P.args, **kwargs: P.kwargs) -> Response:
        data = func(request, *args, **kwargs)
        body = _serialize(data)
        etag_value = f'"{hashlib.md5(body).hexdigest()}"'
        headers = {"ETag": etag_value, "Cache-Control": "no-cache"}
        if_none_match_lines = request.headers.getlist("if-none-match")
        if if_none_match_lines and any(
            _matches_if_none_match(line, etag_value) for line in if_none_match_lines
        ):
            return Response(status_code=304, headers=headers)
        return Response(content=body, status_code=200, media_type="application/json", headers=headers)

    return wrapper
