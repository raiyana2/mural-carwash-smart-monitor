"""app.py: the web interface only (FastAPI routes and the browser page in static/).

Every route hands its work to MonitorController (carwash/controllers/monitor.py).
When the controller refuses something it raises ControllerError, which is turned
into an HTTP error here. Run it from the pi folder:

    python -m uvicorn carwash.web.app:app --host 127.0.0.1 --port 8000
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from carwash.controllers import ControllerError, MonitorController
from carwash.web.schemas import (
    ConnectRequest,
    EnabledRequest,
    PreviewRequest,
    ScenarioRequest,
    SessionRequest,
    SettingsRequest,
)

STATIC_DIR = Path(__file__).parent / "static"
ERROR_STATUS = {"not_found": 404, "invalid": 422, "conflict": 409, "unavailable": 503}

controller = MonitorController()


@asynccontextmanager
async def lifespan(_: FastAPI):
    controller.start()
    try:
        yield
    finally:
        controller.close()


app = FastAPI(title="Mural Car Wash Smart Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(ControllerError)
async def controller_error(_: Request, exc: ControllerError) -> JSONResponse:
    return JSONResponse(status_code=ERROR_STATUS.get(exc.kind, 400), content={"detail": str(exc)})


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard() -> dict:
    return controller.dashboard()


@app.get("/api/bays/{bay_number}")
def bay_detail(bay_number: int) -> dict:
    return controller.bay_detail(bay_number)


@app.put("/api/bays/{bay_number}/settings")
def update_settings(bay_number: int, request: SettingsRequest) -> dict:
    values = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    return controller.update_settings(bay_number, values)


@app.put("/api/bays/{bay_number}/enabled")
def set_enabled(bay_number: int, request: EnabledRequest) -> dict:
    return controller.set_enabled(bay_number, request.enabled)


@app.post("/api/bays/{bay_number}/start")
def start_session(bay_number: int, request: SessionRequest) -> dict:
    return controller.start_session(bay_number, request.seconds, request.wash_mode)


@app.post("/api/bays/{bay_number}/stop")
def stop_session(bay_number: int) -> dict:
    return controller.stop_session(bay_number)


@app.get("/api/hardware/ports")
def hardware_ports() -> list[dict]:
    return controller.serial_ports()


@app.post("/api/hardware/connect")
def connect_hardware(request: ConnectRequest) -> dict:
    return controller.connect_hardware(request.port)


@app.post("/api/hardware/disconnect")
def disconnect_hardware() -> dict:
    return controller.disconnect_hardware()


@app.post("/api/hardware/ping")
def ping_hardware() -> dict:
    return controller.ping_hardware()


@app.post("/api/pricing/preview")
def pricing_preview(request: PreviewRequest) -> dict:
    return controller.preview(request.bay_number, request.seconds)


@app.post("/api/simulator/scenario")
def simulator_scenario(request: ScenarioRequest) -> dict:
    return controller.trigger_scenario(request.bay_number, request.scenario)


@app.get("/api/analytics")
def analytics() -> dict:
    return controller.analytics()


@app.get("/api/faults")
def faults() -> list[dict]:
    return controller.recent_faults(100)


@app.post("/api/faults/{fault_id}/resolve")
def resolve_fault(fault_id: int) -> dict:
    return controller.resolve_fault(fault_id)
