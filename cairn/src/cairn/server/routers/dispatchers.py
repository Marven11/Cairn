from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
import yaml

from cairn.dispatcher.config import DispatchConfig, validate_dispatcher_name

router = APIRouter(prefix="/dispatchers", tags=["dispatchers"])


class DispatcherEntry(BaseModel):
    name: str
    error: str | None = None


class DispatcherYaml(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str


def _path(request: Request, name: str) -> Path:
    try:
        validate_dispatcher_name(name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return request.app.state.database_path.parent / "dispatcher" / f"{name}.yaml"


def _exists(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise HTTPException(400, "dispatcher config must be a regular file")
    return True


def _read(path: Path) -> str:
    if not _exists(path):
        raise HTTPException(404, "dispatcher config not found")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8", newline="") as source:
            return source.read()
    except (OSError, UnicodeError) as exc:
        raise HTTPException(400, "dispatcher config could not be read") from exc


def _validate(source: str) -> DispatchConfig:
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise HTTPException(422, f"invalid YAML{location}") from exc
    try:
        return DispatchConfig.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_context=False, include_input=False, include_url=False)
        ) from exc


def _write(path: Path, source: str, *, create: bool) -> None:
    exists = _exists(path)
    if create and exists:
        raise HTTPException(409, "dispatcher config already exists")
    if not create and not exists:
        raise HTTPException(404, "dispatcher config not found")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dispatcher-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
            target.write(source)
            target.flush()
            os.fsync(target.fileno())
        if create:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise HTTPException(409, "dispatcher config already exists") from exc
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@router.get("", response_model=list[DispatcherEntry])
def list_dispatchers(request: Request):
    with request.app.state.dispatcher_config_lock:
        entries = []
        for path in (request.app.state.database_path.parent / "dispatcher").glob("*.yaml"):
            entry = DispatcherEntry(name=path.stem)
            try:
                validate_dispatcher_name(path.stem)
                if not _exists(path):
                    continue
            except ValueError as exc:
                entry.error = str(exc)
            except HTTPException as exc:
                entry.error = str(exc.detail)
            entries.append(entry)
        return sorted(entries, key=lambda entry: entry.name)


@router.get("/{name}")
def get_dispatcher(request: Request, name: str):
    path = _path(request, name)
    with request.app.state.dispatcher_config_lock:
        return _validate(_read(path)).model_dump(mode="json")


@router.get("/{name}/yaml", response_model=DispatcherYaml)
def get_dispatcher_yaml(request: Request, name: str):
    path = _path(request, name)
    with request.app.state.dispatcher_config_lock:
        return DispatcherYaml(yaml=_read(path))


@router.post("/{name}", status_code=201, response_model=DispatcherYaml)
def create_dispatcher(request: Request, name: str, body: DispatcherYaml):
    path = _path(request, name)
    _validate(body.yaml)
    with request.app.state.dispatcher_config_lock:
        _write(path, body.yaml, create=True)
    return body


@router.put("/{name}", response_model=DispatcherYaml)
def update_dispatcher(request: Request, name: str, body: DispatcherYaml):
    path = _path(request, name)
    _validate(body.yaml)
    with request.app.state.dispatcher_config_lock:
        _write(path, body.yaml, create=False)
    return body


@router.delete("/{name}", status_code=204)
def delete_dispatcher(request: Request, name: str):
    path = _path(request, name)
    with request.app.state.dispatcher_config_lock:
        if not _exists(path):
            raise HTTPException(404, "dispatcher config not found")
        path.unlink()
    return Response(status_code=204)
