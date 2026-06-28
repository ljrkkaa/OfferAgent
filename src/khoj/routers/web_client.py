from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.authentication import requires

from khoj.routers.helpers import get_next_url
from khoj.utils import constants, state

# Initialize Router
web_client = APIRouter()
templates = Jinja2Templates([constants.web_directory, constants.next_js_directory, constants.pypi_static_directory])
home_templates = Jinja2Templates([constants.home_directory])


# Create Routes
@web_client.get("/", response_class=FileResponse)
def index(request: Request):
    # Redirect unauthenticated users to /home landing page when not in anonymous mode
    # Skip redirect if user explicitly navigated from home page (indicated by query param)
    if not state.anonymous_mode and not request.user.is_authenticated:
        if "v" not in request.query_params:
            return RedirectResponse(url="/home")
    return templates.TemplateResponse(request, name="index.html")


@web_client.post("/", response_class=FileResponse)
@requires(["authenticated"], redirect="login_page")
def index_post(request: Request):
    return templates.TemplateResponse(request, name="index.html")


@web_client.get("/home", response_class=HTMLResponse)
def home_page(request: Request):
    """Serve the landing page for unauthenticated users"""
    # If user is authenticated, redirect to main app
    if request.user.is_authenticated:
        return RedirectResponse(url="/")
    return home_templates.TemplateResponse(request, name="index.html")


@web_client.get("/home/{file_path:path}", response_class=FileResponse)
def home_static_files(file_path: str):
    """Serve static files from the home landing page directory"""
    resolved = (constants.home_directory / file_path).resolve()
    if not resolved.is_relative_to(constants.home_directory.resolve()):
        raise HTTPException(status_code=404)
    return FileResponse(resolved)


@web_client.get("/search", response_class=FileResponse)
@requires(["authenticated"], redirect="login_page")
def search_page(request: Request):
    return templates.TemplateResponse(request, name="search/index.html")


@web_client.get("/chat", response_class=FileResponse)
@requires(["authenticated"], redirect="login_page")
def chat_page(request: Request):
    return templates.TemplateResponse(request, name="chat/index.html")


@web_client.get("/login", response_class=FileResponse)
def login_page(request: Request):
    next_url = get_next_url(request)
    if request.user.is_authenticated:
        return RedirectResponse(url=next_url)
    # Redirect to main app which shows the login popup for unauthenticated users
    # Append v=app to prevent redirect loop back to /home
    redirect_url = f"/?v=app&next={next_url}" if next_url != "/" else "/?v=app"
    return RedirectResponse(url=redirect_url)


@web_client.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    return templates.TemplateResponse(request, name="agents/index.html")


@web_client.get("/settings", response_class=HTMLResponse)
@requires(["authenticated"], redirect="login_page")
def config_page(request: Request):
    return templates.TemplateResponse(request, name="settings/index.html")


@web_client.get("/share/chat/{public_conversation_slug}", response_class=HTMLResponse)
def view_public_conversation(request: Request):
    return templates.TemplateResponse(request, name="share/chat/index.html")


@web_client.get("/automations", response_class=HTMLResponse)
def automations_config_page(
    request: Request,
):
    return templates.TemplateResponse(request, name="automations/index.html")


@web_client.get("/.well-known/assetlinks.json", response_class=FileResponse)
def assetlinks(request: Request):
    return FileResponse(constants.assetlinks_file_path)


@web_client.get("/server/error", response_class=HTMLResponse)
def server_error_page(request: Request):
    return templates.TemplateResponse(request, name="error.html")
