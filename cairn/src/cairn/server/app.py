from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import dispatchers, export, hints, intents, projects, settings

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_folder = app.state.database_path.parent
    data_folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    dispatcher_folder = data_folder / "dispatcher"
    if dispatcher_folder.is_symlink():
        raise ValueError("dispatcher folder must be a real directory")
    dispatcher_folder.mkdir(exist_ok=True, mode=0o700)
    db.configure(app.state.database_path)
    yield


def create_app(data_folder: Path = db.DEFAULT_DATA_FOLDER) -> FastAPI:
    app = FastAPI(
        title="Cairn",
        description="Fact-graph based collaborative exploration protocol",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.database_path = data_folder.expanduser().resolve() / "cairn.db"
    app.state.dispatcher_config_lock = RLock()

    app.include_router(settings.router)
    app.include_router(dispatchers.router)
    app.include_router(projects.router)
    app.include_router(hints.router)
    app.include_router(intents.router)
    app.include_router(export.router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
