import sqlite3
from pathlib import Path

import click
from pydantic import ValidationError
import requests
import uvicorn

from cairn.dispatcher.config import validate_dispatcher_name
from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.migrate import migrate_database


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


def _dispatcher_name(_ctx, _param, value: str) -> str:
    try:
        return validate_dispatcher_name(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--data-folder",
    type=click.Path(file_okay=False, path_type=Path),
    default=str(db.DEFAULT_DATA_FOLDER),
    show_default=True,
    help="Directory for the database and dispatcher configs",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, data_folder: Path, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    from cairn.server.app import create_app

    uvicorn.run(
        create_app(data_folder),
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command("migrate-db")
@click.option(
    "--db-path",
    "source_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Legacy database to convert",
)
@click.option(
    "--target",
    "target_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Output path for the converted database (must not exist)",
)
def migrate_db(source_path: Path, target_path: Path) -> None:
    """Convert a legacy single-goal database to the multi-goal schema."""
    try:
        report = migrate_database(source_path, target_path)
    except (ValueError, sqlite3.Error) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"migrated {source_path} -> {target_path}")
    click.echo(report.summary())


@main.command()
@click.option(
    "--server-url",
    default="http://127.0.0.1:8000",
    show_default=True,
    help="Cairn server base URL",
)
@click.option("--name", required=True, callback=_dispatcher_name, help="Dispatcher config name")
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(server_url: str, name: str, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    try:
        loop = DispatcherLoop(server_url, name)
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(map(str, error['loc'])) if error['loc'] else 'config'}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        raise click.ClickException(f"invalid dispatcher config: {detail}") from exc
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        raise click.ClickException(str(exc)) from exc
