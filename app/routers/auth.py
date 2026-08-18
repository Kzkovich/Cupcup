from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.flash import flash
from app.models import GameProfile, ResetRequest, ResetToken, User
from app.ratelimit import is_rate_limited, register_attempt, reset_attempts
from app.security import (
    generate_recovery_code, generate_reset_token, hash_password,
    hash_token, normalize_recovery_code, verify_password,
)
from app.templating import render

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _issue_recovery_code(user: User, db: Session) -> str:
    code = generate_recovery_code()
    user.recovery_code_hash = hash_token(normalize_recovery_code(code))
    db.commit()
    return code


def _show_recovery_code_and_redirect(request: Request, user: User, db: Session, message: str) -> RedirectResponse:
    code = _issue_recovery_code(user, db)
    request.session["user_id"] = user.id
    request.session["pending_recovery_code"] = code
    flash(request, message, "success")
    return RedirectResponse(url="/auth/recovery-code", status_code=303)


@router.get("/register")
def register_form(request: Request, user: User | None = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/", status_code=303)
    settings = get_settings()
    return render(request, "auth/register.html", user=None, invite_required=bool(settings.invite_code))


@router.post("/register")
def register_submit(
    request: Request,
    db: Session = Depends(get_db),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    display_name: str = Form(""),
    phone: str = Form(""),
    consent_152fz: bool = Form(False),
    invite_code: str = Form(""),
):
    settings = get_settings()
    email = email.strip().lower()
    display_name = display_name.strip()
    phone = phone.strip()
    errors: list[str] = []

    if settings.invite_code and invite_code.strip() != settings.invite_code:
        errors.append("Неверный код приглашения")
    if "@" not in email or len(email) < 5:
        errors.append("Похоже, это не почта")
    if len(password) < 8:
        errors.append("Пароль должен быть не короче 8 символов")
    if password != password2:
        errors.append("Пароли не совпадают")
    if (display_name or phone) and not consent_152fz:
        errors.append("Чтобы сохранить имя/телефон, нужно согласие на обработку персональных данных")

    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing:
        errors.append("Такая почта уже зарегистрирована — попробуйте войти или восстановить доступ")

    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(
            request, "auth/register.html", status_code=400, user=None,
            invite_required=bool(settings.invite_code),
            form={"email": email, "display_name": display_name, "phone": phone},
        )

    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or None,
        phone=phone or None,
        consent_152fz_at=datetime.utcnow() if consent_152fz else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if user.email in settings.admin_emails:
        user.is_admin = True
        db.commit()

    return _show_recovery_code_and_redirect(
        request, user, db, "Аккаунт создан. Сохраните код восстановления — он понадобится, если забудете пароль."
    )


@router.get("/recovery-code")
def show_recovery_code(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    code = request.session.pop("pending_recovery_code", None)
    if not code:
        return RedirectResponse(url="/", status_code=303)
    return render(request, "auth/recovery_code.html", user=user, code=code)


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user), next: str = "/"):
    if user:
        return RedirectResponse(url=next or "/", status_code=303)
    return render(request, "auth/login.html", user=None, next=next)


@router.post("/login")
def login_submit(
    request: Request, db: Session = Depends(get_db),
    email: str = Form(...), password: str = Form(...), next: str = Form("/"),
):
    email = email.strip().lower()
    ip_key = f"login:ip:{_client_ip(request)}"
    email_key = f"login:email:{email}"

    if is_rate_limited(ip_key, email_key):
        flash(request, "Слишком много попыток входа. Подождите 15 минут.", "error")
        return render(request, "auth/login.html", status_code=429, user=None, next=next)

    user = db.query(User).filter(func.lower(User.email) == email).first()
    if not user or not verify_password(password, user.password_hash) or user.banned_at is not None:
        register_attempt(ip_key, email_key)
        flash(request, "Неверная почта или пароль", "error")
        return render(request, "auth/login.html", status_code=400, user=None, next=next, form={"email": email})

    reset_attempts(ip_key, email_key)
    user.last_seen_at = datetime.utcnow()
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse(url=next or "/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "auth/forgot.html", user=None)


@router.post("/forgot")
def forgot_submit(
    request: Request, db: Session = Depends(get_db),
    email: str = Form(...), code: str = Form(...),
):
    email = email.strip().lower()
    ip_key = f"forgot:ip:{_client_ip(request)}"
    email_key = f"forgot:email:{email}"

    if is_rate_limited(ip_key, email_key):
        flash(request, "Слишком много попыток. Подождите 15 минут или напишите организаторам.", "error")
        return render(request, "auth/forgot.html", status_code=429, user=None)

    user = db.query(User).filter(func.lower(User.email) == email).first()
    normalized = normalize_recovery_code(code)
    ok = bool(user and user.recovery_code_hash and hash_token(normalized) == user.recovery_code_hash)

    if not ok:
        register_attempt(ip_key, email_key)
        flash(request, "Почта или код восстановления не совпадают", "error")
        return render(request, "auth/forgot.html", status_code=400, user=None, form={"email": email})

    reset_attempts(ip_key, email_key)
    return _after_verified_go_set_password(request, user)


def _after_verified_go_set_password(request: Request, user: User) -> RedirectResponse:
    request.session["user_id"] = user.id
    request.session["must_set_new_password"] = True
    flash(request, "Код подошёл. Задайте новый пароль.", "success")
    return RedirectResponse(url="/auth/change-password", status_code=303)


@router.get("/change-password")
def change_password_form(request: Request, user: User | None = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/auth/login?next=/auth/change-password", status_code=303)
    forced = bool(request.session.get("must_set_new_password"))
    return render(request, "auth/change_password.html", user=user, forced=forced)


@router.post("/change-password")
def change_password_submit(
    request: Request, db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user),
    current_password: str = Form(""),
    new_password: str = Form(...),
    new_password2: str = Form(...),
):
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)
    forced = bool(request.session.get("must_set_new_password"))

    if not forced and not verify_password(current_password, user.password_hash):
        flash(request, "Текущий пароль неверный", "error")
        return render(request, "auth/change_password.html", status_code=400, user=user, forced=forced)
    if len(new_password) < 8:
        flash(request, "Новый пароль должен быть не короче 8 символов", "error")
        return render(request, "auth/change_password.html", status_code=400, user=user, forced=forced)
    if new_password != new_password2:
        flash(request, "Пароли не совпадают", "error")
        return render(request, "auth/change_password.html", status_code=400, user=user, forced=forced)

    user.password_hash = hash_password(new_password)
    db.commit()
    request.session.pop("must_set_new_password", None)
    return _show_recovery_code_and_redirect(
        request, user, db, "Пароль обновлён. Вот новый код восстановления — сохраните его."
    )


@router.get("/cant-login")
def cant_login_form(request: Request):
    settings = get_settings()
    return render(request, "auth/cant_login.html", user=None, org_contact=settings.org_contact_text)


@router.post("/cant-login")
def cant_login_submit(
    request: Request, db: Session = Depends(get_db),
    identifier: str = Form(...), contact_note: str = Form(...),
):
    settings = get_settings()
    identifier = identifier.strip()
    contact_note = contact_note.strip()
    if not identifier or not contact_note:
        flash(request, "Заполните оба поля", "error")
        return render(request, "auth/cant_login.html", status_code=400, user=None, org_contact=settings.org_contact_text)

    db.add(ResetRequest(identifier=identifier, contact_note=contact_note))
    db.commit()
    flash(request, "Заявка отправлена организаторам, с вами свяжутся.", "success")
    return RedirectResponse(url="/auth/cant-login", status_code=303)


@router.get("/reset/{token}")
def reset_by_token_form(request: Request, token: str, db: Session = Depends(get_db)):
    reset_token = db.query(ResetToken).filter(ResetToken.token_hash == hash_token(token)).first()
    if not reset_token or reset_token.used_at or reset_token.expires_at < datetime.utcnow():
        flash(request, "Ссылка недействительна или уже использована. Запросите новую у организаторов.", "error")
        return RedirectResponse(url="/auth/cant-login", status_code=303)
    return render(request, "auth/reset_token.html", user=None, token=token)


@router.post("/reset/{token}")
def reset_by_token_submit(
    request: Request, token: str, db: Session = Depends(get_db),
    new_password: str = Form(...), new_password2: str = Form(...),
):
    reset_token = db.query(ResetToken).filter(ResetToken.token_hash == hash_token(token)).first()
    if not reset_token or reset_token.used_at or reset_token.expires_at < datetime.utcnow():
        flash(request, "Ссылка недействительна или уже использована. Запросите новую у организаторов.", "error")
        return RedirectResponse(url="/auth/cant-login", status_code=303)

    if len(new_password) < 8:
        flash(request, "Пароль должен быть не короче 8 символов", "error")
        return render(request, "auth/reset_token.html", status_code=400, user=None, token=token)
    if new_password != new_password2:
        flash(request, "Пароли не совпадают", "error")
        return render(request, "auth/reset_token.html", status_code=400, user=None, token=token)

    user = db.get(User, reset_token.user_id)
    user.password_hash = hash_password(new_password)
    reset_token.used_at = datetime.utcnow()
    db.commit()

    return _show_recovery_code_and_redirect(
        request, user, db, "Пароль обновлён. Вот новый код восстановления — сохраните его."
    )
