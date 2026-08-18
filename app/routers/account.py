from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_last_game, require_user
from app.flash import flash
from app.models import User
from app.telegram_bot import deep_link, generate_link_code
from app.templating import render

router = APIRouter(tags=["account"])


@router.get("/account")
def account_form(request: Request, user: User = Depends(require_user)):
    tg_link = request.session.pop("tg_link", None)
    return render(
        request, "account.html", user=user, game=get_last_game(request),
        telegram_enabled=get_settings().telegram_enabled, tg_link=tg_link,
    )


@router.post("/account")
def account_submit(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user),
    display_name: str = Form(""), phone: str = Form(""),
    consent_152fz: bool = Form(False), contact_note: str = Form(""),
):
    display_name = display_name.strip()
    phone = phone.strip()
    contact_note = contact_note.strip()

    if (display_name or phone) and not consent_152fz:
        flash(request, "Чтобы сохранить имя/телефон, нужно согласие на обработку персональных данных", "error")
        return render(request, "account.html", status_code=400, user=user, game=get_last_game(request))

    user.display_name = display_name or None
    user.phone = phone or None
    user.contact_note = contact_note or None
    if consent_152fz and not user.consent_152fz_at:
        user.consent_152fz_at = datetime.utcnow()
    if not (display_name or phone):
        user.consent_152fz_at = None

    db.commit()
    flash(request, "Данные сохранены", "success")
    return RedirectResponse(url="/account", status_code=303)


@router.post("/account/telegram/link")
def telegram_link(request: Request, user: User = Depends(require_user)):
    if not get_settings().telegram_enabled:
        flash(request, "Telegram-уведомления сейчас отключены организаторами", "info")
        return RedirectResponse(url="/account", status_code=303)
    code = generate_link_code(user.id)
    request.session["tg_link"] = deep_link(code)
    return RedirectResponse(url="/account", status_code=303)


@router.post("/account/telegram/unlink")
def telegram_unlink(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    user.tg_chat_id = None
    user.tg_username = None
    db.commit()
    flash(request, "Telegram отключён", "success")
    return RedirectResponse(url="/account", status_code=303)
