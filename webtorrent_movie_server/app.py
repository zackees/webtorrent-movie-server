"""
    Flask app for the ytclip command line tool. Serves an index.html at port 80. Clipping
    api is located at /clip
"""
import datetime
import os
import subprocess
import threading

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from keyvalue_sqlite import KeyValueSqlite  # type: ignore

from webtorrent_movie_server.version import VERSION

print("Starting fastapi webtorrent movie server")

DEFAULT_TRACKER_URL = "wss://webtorrent-tracker.onrender.com"
TRACKER_URL = os.environ.get("TRACKER_URL", DEFAULT_TRACKER_URL)
CLIENT_SEED_PORT = 8000

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
app_state = KeyValueSqlite(os.path.join(DATA_DIR, "app.sqlite"), "app")

app = FastAPI()

RUNNING_MOVIE_PROCESS: subprocess.Popen | None = None

STARTUP_DATETIME = datetime.datetime.now()

PASSWORD = os.environ.get(
    "WEBTORRENT_MOVIE_SERVER_PASSWORD"
)  # TODO: implement this  # pylint: disable=fixme


LOGFILE = os.path.join(DATA_DIR, "log.txt")
LOG = open(LOGFILE, encoding="utf-8", mode="a")


def log_error(msg: str) -> None:
    """Logs an error to the print stream."""
    print(msg)


def get_current_thread_id() -> int:
    """Return the current thread id."""
    ident = threading.current_thread().ident
    if ident is None:
        return -1
    return int(ident)


@app.on_event("startup")
async def startup_event():
    """Event handler for when the app starts up."""
    print("Startup event")
    LOG.write(f"Startup event\n")


@app.on_event("shutdown")
def shutdown_event():
    """Event handler for when the app shuts down."""
    print("Application shutdown")
    LOG.write(f"Application shutdown\n")
    LOG.close()


# Mount all the static files.
app.mount("/www", StaticFiles(directory=os.path.join(HERE, "www")), "www")


# Redirect to index.html
# @app.get("/")
# async def index(request: Request) -> RedirectResponse:
#    """Returns index.html file"""
#    params = {item[0]: item[1] for item in request.query_params.multi_items()}
#    query = ""
#    for key, value in params.items():
#        if query == "":
#            query += "?"
#        query += f"{key}={value}&"
#    return RedirectResponse(url=f"/www/index.html{query}", status_code=302)


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    """By default redirect to the fastapi docs."""
    return RedirectResponse(url="/docs", status_code=302)


# Redirect to favicon.ico
@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    """Returns favico file."""
    return RedirectResponse(url="/www/favicon.ico")


def seed_movie(file_path: str) -> str:
    """Callback for when a new movie is added."""

    print(f"New movie added: {file_path}")
    cwd = os.path.dirname(file_path)
    file_name = os.path.basename(file_path)
    cmd = (
        f'webtorrent-hybrid seed --keep-seeding "{file_name}"'
        f" --announce {TRACKER_URL} --port {CLIENT_SEED_PORT}"
    )
    print(f"Running: {cmd}")
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, universal_newlines=True
    )
    magnet_uri = None
    for line in iter(process.stdout.readline, ""):  # type: ignore
        print(line, end="")
        if line.startswith("magnetURI: "):
            magnet_uri = line.split(" ")[1].strip()
            print(f"Found magnetURI: {magnet_uri}")
            app_state.set("magnetURI", magnet_uri)
            break

    # Make sure that the stdout buffer is drained, or else the process
    # may freeze.
    def drain_stdout(process: subprocess.Popen) -> None:
        for line in iter(process.stdout.readline, ""):  # type: ignore
            print(line, end="")

    threading.Thread(target=drain_stdout, args=(process,)).start()
    assert magnet_uri is not None, "Could not find magnet URI"
    # Do something
    return magnet_uri


@app.post("/upload")
async def upload(file: UploadFile = File(...)) -> PlainTextResponse:
    """Uploads a file to the server."""
    if not file.filename.endswith(".mp4"):
        return PlainTextResponse(status_code=410, content="Invalid file type, must be mp4")
    tmp_dest_path = os.path.join(DATA_DIR, "tmp_" + os.urandom(16).hex() + ".mp4")
    final_path = os.path.join(DATA_DIR, file.filename)
    exc_string: str | None = None  # exception string, if it happens.
    exc_status_code: int = 0  # http status code for exception, if it happens.
    magnet_uri = None
    try:
        # Generate a random name for the temp file.
        with open(tmp_dest_path, mode="wb") as filed:
            while (chunk := await file.read(1024 * 64)) != b"":
                filed.write(chunk)
        # After writing is finished, move the file to the final location.
        if not os.path.exists(final_path):
            os.rename(tmp_dest_path, final_path)
        else:
            if os.path.getsize(final_path) != os.path.getsize(tmp_dest_path):
                raise OSError("A file already exists with this file name but is different.")
            os.remove(tmp_dest_path)
        magnet_uri = seed_movie(final_path)
    except Exception as err:  # pylint: disable=broad-except
        exc_string = "There was an error uploading the file because: " + str(err)
        exc_status_code = 500
    finally:
        await file.close()
        if os.path.exists(tmp_dest_path):
            try:
                os.remove(tmp_dest_path)
            except OSError as os_err:
                exc_status_code = 500
                exc_string = "There was an error deleting the temp file because: " + str(os_err)
    if exc_string is not None:
        return PlainTextResponse(status_code=exc_status_code, content=exc_string)
    return PlainTextResponse(content=magnet_uri)


@app.get("/accessMagnetURI")
async def api_add_view(add_view: bool = True) -> JSONResponse:
    """Get the stored magnet URI and optionally increment the number of views."""
    if add_view:
        app_state.atomic_add("views", 1)
    out = {
        "views": app_state.get("views", 0),
        "magnetURI": app_state.get("magnetURI", "None"),
        "add_view": add_view,
    }
    return JSONResponse(content=out)


@app.get("/info")
async def api_info() -> JSONResponse:
    """Returns the current time and the number of seconds since the server started."""
    app_data = app_state.to_dict()
    out = {
        "version": VERSION,
        "Launched at": str(STARTUP_DATETIME),
        "Current utc time": str(datetime.datetime.utcnow()),
        "Process ID": os.getpid(),
        "Thread ID": get_current_thread_id(),
        "Number of Views": app_data.get("views", 0),
        "App state": app_data,
    }
    return JSONResponse(out)


@app.get("/stats")
async def api_views() -> JSONResponse:
    """Returns the current number of views."""
    app_data = app_state.to_dict()
    views = str(app_data.get("views", 0))
    return JSONResponse({"views": views})


def touch(fname):
    """Touches file"""
    open(fname, encoding="utf-8", mode="a").close()  # pylint: disable=consider-using-with
    os.utime(fname, None)


@app.get("/clear")
async def clear() -> PlainTextResponse:
    """Clears the stored magnet URI."""
    app_state.clear()
    # use os.touch to trigger a restart on this server.
    touch(os.path.join(ROOT, "restart.file"))
    return PlainTextResponse(content="Server queued for restart.")


print("Starting fastapi webtorrent movie server loaded.")
