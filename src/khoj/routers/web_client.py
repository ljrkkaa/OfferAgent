from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from khoj.utils import constants

# Initialize Router
web_client = APIRouter()
templates = Jinja2Templates([constants.web_directory, constants.next_js_directory, constants.pypi_static_directory])


# Create Routes
@web_client.get("/", response_class=FileResponse)
def index(request: Request):
    return templates.TemplateResponse(request, name="index.html")


@web_client.post("/", response_class=FileResponse)
def index_post(request: Request):
    return templates.TemplateResponse(request, name="index.html")


@web_client.get("/search", response_class=FileResponse)
def search_page(request: Request):
    return templates.TemplateResponse(request, name="search/index.html")


@web_client.get("/chat", response_class=FileResponse)
def chat_page(request: Request):
    return templates.TemplateResponse(request, name="chat/index.html")


@web_client.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    return templates.TemplateResponse(request, name="agents/index.html")


@web_client.get("/settings", response_class=HTMLResponse)
def config_page(request: Request):
    return templates.TemplateResponse(request, name="settings/index.html")


@web_client.get("/automations", response_class=HTMLResponse)
def automations_config_page(
    request: Request,
):
    return templates.TemplateResponse(request, name="automations/index.html")


@web_client.get("/server/error", response_class=HTMLResponse)
def server_error_page(request: Request):
    return templates.TemplateResponse(request, name="error.html")


def _next_export_text_file(file_path: str):
    built_dir = constants.next_js_directory.resolve()
    stem = file_path.removesuffix(".txt")
    candidates = [
        (built_dir / file_path).resolve(),
        (built_dir / stem / "index.txt").resolve(),
    ]

    for candidate in candidates:
        if candidate.is_file() and candidate.is_relative_to(built_dir):
            return FileResponse(candidate, media_type="text/plain")

    raise HTTPException(status_code=404)


@web_client.get("/index.txt", response_class=FileResponse)
def next_export_index_text_file():
    return _next_export_text_file("index.txt")


@web_client.get("/{page_name}.txt", response_class=FileResponse)
def next_export_page_text_file(page_name: str):
    return _next_export_text_file(f"{page_name}.txt")
