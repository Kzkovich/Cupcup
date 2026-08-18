import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.deps import AdminRequired, AuthRequired, get_current_user
from app.routers import account as account_router
from app.routers import admin as admin_router
from app.routers import agents as agents_router
from app.routers import applications as applications_router
from app.routers import auth as auth_router
from app.routers import notifications as notifications_router
from app.routers import profile as profile_router
from app.routers import teams as teams_router
from app.telegram_bot import poll_loop
from app.templating import render

settings = get_settings()
logger = logging.getLogger("alfacybercup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.telegram_enabled:
        task = asyncio.create_task(poll_loop())
        logger.info("Telegram polling task started")
    yield
    if task:
        task.cancel()


app = FastAPI(title="AlfaCyberCup", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="acc_session",
    max_age=90 * 24 * 3600,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.exception_handler(AuthRequired)
async def _auth_required(request: Request, exc: AuthRequired):
    return RedirectResponse(url=f"/auth/login?next={exc.next_path}", status_code=303)


@app.exception_handler(AdminRequired)
async def _admin_required(request: Request, exc: AdminRequired):
    return RedirectResponse(url="/", status_code=303)


app.include_router(auth_router.router)
app.include_router(account_router.router)
app.include_router(profile_router.router)
app.include_router(teams_router.router)
app.include_router(applications_router.router)
app.include_router(agents_router.router)
app.include_router(notifications_router.router)
app.include_router(admin_router.router)


@app.get("/")
def landing(request: Request, user=Depends(get_current_user)):
    return render(request, "landing.html", user=user, game=None)


@app.get("/healthz")
def healthz():
    return {"ok": True}
