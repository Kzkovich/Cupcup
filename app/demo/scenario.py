"""Сквозной прогон всех сценариев через HTTP-слой приложения.

Запускается только в изолированном процессе на временной БД (app.demo.scenario_runner),
поэтому не пересекается с реальными данными. Напрямую не вызывать на боевой базе:
без SCENARIO_ISOLATED=1 модуль откажется работать.
"""
import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass

if os.environ.get("SCENARIO_ISOLATED") != "1":
    raise SystemExit("Сценарий запускается только через app.demo.scenario_runner (изолированная БД)")

import bcrypt  # noqa: E402

_orig_gensalt = bcrypt.gensalt
bcrypt.gensalt = lambda rounds=4, prefix=b"2b": _orig_gensalt(rounds=4, prefix=prefix)  # быстрее в сотни раз

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import GameProfile, Team, TeamSlot, User  # noqa: E402
from app.ratelimit import reset_attempts  # noqa: E402

ADMIN_EMAIL = os.environ.get("ADMIN_EMAILS", "qa.admin@mail.invalid").split(",")[0]
PASSWORD = "scenario-pass-1"


@dataclass
class Step:
    section: str
    name: str
    ok: bool
    detail: str = ""


class Recorder:
    def __init__(self):
        self.steps: list[Step] = []
        self.section = ""

    def sec(self, title: str):
        self.section = title

    def check(self, name: str, cond, detail: str = "") -> bool:
        self.steps.append(Step(self.section, name, bool(cond), "" if cond else str(detail)[:300]))
        return bool(cond)


R = Recorder()


def new_client() -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def loc(r) -> str:
    return r.headers.get("location", "")


def flashes(html: str) -> list[str]:
    return re.findall(r'class="flash flash-[a-z]+">([^<]*)<', html)


def badge(html: str) -> int:
    m = re.search(r'badge-count">(\d+)<', html)
    return int(m.group(1)) if m else 0


class Actor:
    def __init__(self, key: str):
        self.key = key
        self.email = f"{key}@mail.invalid" if key != "admin" else ADMIN_EMAIL
        self.nick = f"QA_{key}"
        self.c = new_client()
        self.code = None

    def register(self, name: str | None = None, phone: str | None = None, consent: bool = False):
        data = {"email": self.email, "password": PASSWORD, "password2": PASSWORD,
                "display_name": name or "", "phone": phone or "", "invite_code": ""}
        if consent:
            data["consent_152fz"] = "true"
        r = self.c.post("/auth/register", data=data)
        if r.status_code == 303:
            page = self.c.get(loc(r)).text
            m = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", page)
            self.code = m.group(0) if m else None
        return r

    def login(self, password: str = PASSWORD):
        return self.c.post("/auth/login", data={"email": self.email, "password": password, "next": "/"})

    def profile(self, game: str, rating: str, roles: list[str], region: str = "", looking: bool = True, nick: str | None = None):
        data = {"nickname": nick or self.nick, "account_id": "", "region": region, "profile_url": "",
                "rating": rating, "preferred_roles": roles, "about": "QA"}
        if looking:
            data["is_looking"] = "true"
        return self.c.post(f"/{game}/profile", data=data)

    def get(self, url: str) -> str:
        return self.c.get(url).text


def uid(email: str) -> int:
    with SessionLocal() as s:
        return s.query(User).filter(User.email == email).one().id


def slots(team_id: int) -> dict:
    with SessionLocal() as s:
        return {x.role: {"id": x.id, "state": x.state, "user_id": x.user_id, "note": x.note}
                for x in s.query(TeamSlot).filter(TeamSlot.team_id == team_id)}


def team_row(team_id: int) -> dict:
    with SessionLocal() as s:
        t = s.get(Team, team_id)
        return {"name": t.name, "status": t.status, "captain_id": t.captain_id}


def profile_id(email: str, game: str) -> int:
    with SessionLocal() as s:
        return s.query(GameProfile).filter(GameProfile.user_id == uid(email), GameProfile.game == game).one().id


def team_form(roles: dict, name: str, desc: str = "QA", **extra) -> dict:
    """roles: {'pos1': 'mine', 'pos3': ('reserved', 'друг')}, остальные — open."""
    data = {"name": name, "tag": "QA", "description": desc, "rating_min_wanted": "", "rating_max_wanted": ""}
    data.update(extra)
    for k, v in roles.items():
        kind, note = v if isinstance(v, tuple) else (v, "")
        data[f"role_{k}"] = kind
        data[f"note_{k}"] = note
    return data


DOTA_ROLES = ["pos1", "pos2", "pos3", "pos4", "pos5"]


def create_team(actor: Actor, game: str, name: str, mine: str, reserved: dict | None = None, **extra) -> int | None:
    roles = {r: "open" for r in (DOTA_ROLES if game == "dota2" else ["top", "jungle", "mid", "adc", "support"])}
    roles[mine] = "mine"
    for k, note in (reserved or {}).items():
        roles[k] = ("reserved", note)
    r = actor.c.post(f"/{game}/teams/new", data=team_form(roles, name, **extra))
    m = re.search(rf"/{game}/teams/(\d+)", loc(r))
    return int(m.group(1)) if r.status_code == 303 and m else None


def run() -> list[Step]:
    Base.metadata.create_all(engine)
    anon = new_client()

    # ------------------------------------------------------------------ 1
    R.sec("1. Доступность сайта")
    for url in ["/", "/healthz", "/dota2/teams", "/lol/teams", "/dota2/agents", "/lol/agents",
                "/auth/login", "/auth/register", "/auth/cant-login", "/static/css/main.css"]:
        r = anon.get(url)
        R.check(f"GET {url} → 200", r.status_code == 200, r.status_code)
    R.check("Главная показывает обе дисциплины",
            all(x in anon.get("/").text for x in ["Dota 2", "League of Legends"]))

    # ------------------------------------------------------------------ 2
    R.sec("2. Регистрация и вход")
    cap = Actor("cap")
    r = cap.register(name="Капитан Тест", phone="+79990000000", consent=False)
    R.check("Имя/телефон без согласия 152-ФЗ отклоняются (400)", r.status_code == 400, r.status_code)
    R.check("Пользователь при этом не создан", not _exists(cap.email))
    r = cap.register(name="Капитан Тест", phone="+79990000000", consent=True)
    R.check("Регистрация с согласием → 303", r.status_code == 303, r.status_code)
    R.check("Показан код восстановления формата XXXX-XXXX-XXXX", bool(cap.code), cap.code)
    R.check("Согласие 152-ФЗ записано в БД", _field(cap.email, "consent_152fz_at") is not None)
    r = new_client().post("/auth/register", data={"email": cap.email, "password": PASSWORD, "password2": PASSWORD})
    R.check("Повторная почта отклонена (400)", r.status_code == 400, r.status_code)
    r = new_client().post("/auth/register", data={"email": "short@mail.invalid", "password": "abc", "password2": "abc"})
    R.check("Короткий пароль отклонён (400)", r.status_code == 400, r.status_code)
    r = new_client().post("/auth/register", data={"email": "mm@mail.invalid", "password": PASSWORD, "password2": "other-pass-1"})
    R.check("Несовпадающие пароли отклонены (400)", r.status_code == 400, r.status_code)
    r = new_client().post("/auth/register", data={"email": "not-an-email", "password": PASSWORD, "password2": PASSWORD})
    R.check("Некорректная почта отклонена (400)", r.status_code == 400, r.status_code)

    r = cap.c.post("/auth/logout")
    R.check("Выход → 303", r.status_code == 303, r.status_code)
    r = cap.login("wrong-password")
    R.check("Неверный пароль → 400", r.status_code == 400, r.status_code)
    r = cap.login()
    R.check("Верный пароль → 303", r.status_code == 303, r.status_code)
    R.check("Cookie сессии HttpOnly + SameSite=lax",
            all(x in r.headers.get("set-cookie", "").lower() for x in ["httponly", "samesite=lax"]),
            r.headers.get("set-cookie", ""))

    rl = Actor("ratelimit")
    rl.register()
    codes = [Actor("ratelimit").login("bad") .status_code for _ in range(5)]
    last = rl.login("bad").status_code
    R.check("5 неверных попыток подряд, 6-я блокируется (429)", last == 429, f"{codes} → {last}")
    reset_attempts("login:ip:testclient", f"login:email:{rl.email}")

    # восстановление по коду
    rec = Actor("recov")
    rec.register()
    old_code = rec.code
    fc = new_client()
    r = fc.post("/auth/forgot", data={"email": rec.email, "code": "AAAA-BBBB-CCCC"})
    R.check("Неверный код восстановления → 400", r.status_code == 400, r.status_code)
    r = fc.post("/auth/forgot", data={"email": rec.email, "code": old_code})
    R.check("Верный код → 303 на смену пароля", r.status_code == 303 and "change-password" in loc(r), loc(r))
    r = fc.post("/auth/change-password", data={"new_password": "brand-new-pass-2", "new_password2": "brand-new-pass-2"})
    page = fc.get(loc(r)).text
    new_code = (re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", page) or [None])[0]
    R.check("После смены пароля выдан НОВЫЙ код восстановления", new_code and new_code != old_code, f"{old_code}→{new_code}")
    R.check("Вход с новым паролем работает", new_client().post(
        "/auth/login", data={"email": rec.email, "password": "brand-new-pass-2", "next": "/"}).status_code == 303)
    R.check("Старый пароль больше не подходит", new_client().post(
        "/auth/login", data={"email": rec.email, "password": PASSWORD, "next": "/"}).status_code == 400)
    r = new_client().post("/auth/forgot", data={"email": rec.email, "code": old_code})
    R.check("Использованный код восстановления не работает повторно", r.status_code == 400, r.status_code)
    rec.code = new_code

    # ------------------------------------------------------------------ 3
    R.sec("3. Профили и изоляция дисциплин")
    r = cap.profile("dota2", "4200", ["pos1", "pos2"])
    R.check("Профиль Dota сохранён → 303", r.status_code == 303, r.status_code)
    R.check("Ник виден на странице профиля", cap.nick in cap.get("/dota2/profile"))
    p1, p2, p3, p4, p5, cap2, ag = (Actor(k) for k in ["p1", "p2", "p3", "p4", "p5", "cap2", "ag"])
    for a in (p1, p2, p3, p4, p5, cap2, ag):
        a.register()
    p1.profile("dota2", "99999", ["pos2"])
    R.check("MMR выше максимума ограничивается 12000",
            'value="12000"' in p1.get("/dota2/profile"), "значение не 12000")
    p1.profile("dota2", "3900", ["pos2"])
    cap2.profile("dota2", "4000", ["pos1"])
    p2.profile("dota2", "3700", ["pos2", "pos4"])
    p3.profile("dota2", "3500", ["pos3"])
    p4.profile("dota2", "3300", ["pos3"])
    p5.profile("dota2", "3100", ["pos5"])
    ag.profile("dota2", "3000", ["pos5"])

    lolc = Actor("lolc")
    lolc.register()
    r = lolc.profile("lol", "gold_ii", ["mid"], region="EUNE")
    R.check("Профиль LoL с тиром Gold II сохранён → 303", r.status_code == 303, r.status_code)
    R.check("Выбранный тир Gold II сохранён и отмечен в форме", 'value="gold_ii" selected' in lolc.get("/lol/profile"))
    r = lolc.profile("lol", "not_a_tier", ["mid"], region="EUNE")
    R.check("Несуществующий тир отклонён (400)", r.status_code == 400, r.status_code)
    r = lolc.profile("lol", "gold_ii", ["mid"], region="")
    R.check("LoL без региона отклонён (400)", r.status_code == 400, r.status_code)
    R.check("Игрок LoL не виден в агентах Dota", lolc.nick not in anon.get("/dota2/agents").text)
    R.check("Игрок LoL виден в агентах LoL", lolc.nick in anon.get("/lol/agents").text)
    R.check("Игрок Dota не виден в агентах LoL", ag.nick not in anon.get("/lol/agents").text)

    # ------------------------------------------------------------------ 4
    R.sec("4. Команды")
    t1 = create_team(cap, "dota2", "QA Команда Альфа", "pos1", reserved={"pos3": "друг Макс"},
                     desc="Играем по вечерам")
    R.check("Создание команды (капитан pos1, pos3 занят другом) → 303", t1 is not None)
    detail = cap.get(f"/dota2/teams/{t1}")
    R.check("На странице команды «Состав (1/5)»", "Состав (1/5)" in detail)
    R.check("Заметка про занятый слот видна", "друг Макс" in detail)
    r = cap.c.get("/dota2/teams/new")
    R.check("Вторую команду в той же дисциплине создать нельзя (редирект)",
            r.status_code == 303 and "/dota2/teams" in loc(r), loc(r))
    R.check("Команда в списке Dota", "QA Команда Альфа" in anon.get("/dota2/teams").text)
    R.check("Команды Dota нет в списке LoL", "QA Команда Альфа" not in anon.get("/lol/teams").text)
    R.check("Фильтр «моя роль = pos2» находит команду", "QA Команда Альфа" in anon.get("/dota2/teams?role=pos2").text)
    R.check("Фильтр по занятой роли (pos3) её скрывает", "QA Команда Альфа" not in anon.get("/dota2/teams?role=pos3").text)

    cap.c.post(f"/dota2/teams/{t1}/edit", data={"name": "QA Команда Альфа", "tag": "QA",
               "description": "<script>alert(1)</script> текст", "rating_min_wanted": "", "rating_max_wanted": ""})
    d = anon.get(f"/dota2/teams/{t1}").text
    R.check("XSS в описании экранируется", "<script>alert(1)" not in d and "&lt;script&gt;" in d)
    p1.c.post(f"/dota2/teams/{t1}/edit", data={"name": "ВЗЛОМ", "tag": "", "description": "",
              "rating_min_wanted": "", "rating_max_wanted": ""})
    R.check("Не-капитан не может переименовать команду", team_row(t1)["name"] == "QA Команда Альфа")
    r = cap.c.post(f"/dota2/teams/{t1}/bump")
    R.check("Повторное «поднять» сразу после создания блокируется кулдауном",
            any("раз в 2 часа" in f for f in flashes(cap.c.get(loc(r)).text)))
    R.check("Несуществующая команда → 404", anon.get("/dota2/teams/999999").status_code == 404)
    R.check("Неизвестная дисциплина → 404", anon.get("/csgo/teams").status_code == 404)

    t_lol = create_team(lolc, "lol", "QA LoL Команда", "mid", rating_min_wanted="gold_iv", rating_max_wanted="gold_i")
    R.check("Команда LoL с диапазоном тиров создана", t_lol is not None)
    R.check("LoL: фильтр рейтинга от Gold III включает команду",
            "QA LoL Команда" in anon.get("/lol/teams?rating_min=gold_iii").text)
    R.check("LoL: фильтр рейтинга от Diamond IV исключает команду",
            "QA LoL Команда" not in anon.get("/lol/teams?rating_min=diamond_iv").text)

    # ------------------------------------------------------------------ 5
    R.sec("5. Заявки")
    p1.c.post("/account", data={"contact_note": "Telegram — тест", "display_name": "", "phone": ""})
    r = p1.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos2"], "message": "Хочу играть"})
    R.check("Заявка на открытую роль → 303", r.status_code == 303)
    r = p1.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos2"], "message": "ещё раз"})
    R.check("Дубль заявки блокируется",
            any("уже подали" in f for f in flashes(p1.c.get(loc(r)).text)))
    r = p2.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos3"], "message": "на занятую"})
    R.check("Заявка на занятую (reserved) роль не создаётся",
            any("открытую роль" in f for f in flashes(p2.c.get(loc(r)).text)))
    r = cap.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos4"]})
    R.check("Капитан не может подать заявку в свою команду", _apps(cap.email, t1) == 0, _apps(cap.email, t1))

    me_cap = cap.get("/dota2/me")
    R.check("Капитан видит входящую заявку с ником игрока", p1.nick in me_cap)
    R.check("Капитан видит контакт заявителя", "Telegram — тест" in me_cap)
    R.check("В шапке капитана есть счётчик непрочитанных", badge(cap.get("/dota2/teams")) >= 1)
    app_id = _find_id(me_cap, r"/applications/(\d+)/accept")
    r = p3.c.post(f"/applications/{app_id}/accept") if app_id else None
    R.check("Чужой игрок не может принять заявку", app_id and _app_status(app_id) == "pending")
    r = cap2.c.post(f"/applications/{app_id}/accept")
    R.check("Капитан другой команды не может принять чужую заявку", _app_status(app_id) == "pending")
    r = cap.c.post(f"/applications/{app_id}/accept")
    R.check("Капитан принимает заявку → 303", r.status_code == 303)
    R.check("Слот pos2 занят заявителем", slots(t1)["pos2"]["state"] == "filled" and slots(t1)["pos2"]["user_id"] == uid(p1.email))
    R.check("Игрок получил уведомление о принятии", "Вас приняли" in p1.get("/notifications"))

    # авто-отзыв параллельных заявок
    t2 = create_team(cap2, "dota2", "QA Команда Бета", "pos1")
    p2.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos4"], "message": "A"})
    p2.c.post(f"/dota2/teams/{t2}/apply", data={"roles": ["pos2"], "message": "B"})
    R.check("Игрок подал заявки сразу в две команды", p2.get("/dota2/me").count("на рассмотрении") == 2)
    me_cap = cap.get("/dota2/me")
    pending_id = _find_id(me_cap, r"/applications/(\d+)/accept")
    cap.c.post(f"/applications/{pending_id}/accept")
    me_p2 = p2.get("/dota2/me")
    R.check("Параллельная заявка автоматически отозвана", "отозвана автоматически" in me_p2)
    R.check("Капитан второй команды уведомлён об авто-отзыве", "отозвана автоматически" in cap2.get("/notifications"))

    # отклонение и отзыв
    p3.c.post(f"/dota2/teams/{t2}/apply", data={"roles": ["pos3"], "message": "rej"})
    aid = _find_id(cap2.get("/dota2/me"), r"/applications/(\d+)/reject")
    cap2.c.post(f"/applications/{aid}/reject")
    R.check("Отклонённая заявка отображается у игрока", "отклонена" in p3.get("/dota2/me"))
    R.check("Игрок получил уведомление об отказе", "отклонена" in p3.get("/notifications"))
    p3.c.post(f"/dota2/teams/{t2}/apply", data={"roles": ["pos3"], "message": "again"})
    aid = _find_id(p3.get("/dota2/me"), r"/applications/(\d+)/withdraw")
    p3.c.post(f"/applications/{aid}/withdraw")
    R.check("Игрок может отозвать свою заявку", _app_status(aid) == "withdrawn")

    # ------------------------------------------------------------------ 6
    R.sec("6. Свободные игроки и приглашения")
    R.check("Свободный агент виден в списке", ag.nick in anon.get("/dota2/agents").text)
    R.check("Фильтр агентов по роли pos5 находит игрока", ag.nick in anon.get("/dota2/agents?role=pos5").text)
    R.check("Фильтр по другой роли его скрывает", ag.nick not in anon.get("/dota2/agents?role=pos1").text)
    agent_list = anon.get("/dota2/agents?role=pos5&view=list").text
    R.check("Список игроков сохраняет фильтр и режим списка", 'directory-list' in agent_list and ag.nick in agent_list)
    agent_url = f"/dota2/agents/{uid(ag.email)}"
    R.check("Из каталога можно открыть игрока", agent_url in agent_list)
    agent_page = anon.get(agent_url).text
    R.check("Публичный профиль показывает игрока без почты", ag.nick in agent_page and ag.email not in agent_page)
    R.check("Профиль игрока не смешивает дисциплины", anon.get(f"/lol/agents/{uid(ag.email)}").status_code == 404)
    R.check("Капитан видит приглашение в профиле игрока", 'Отправить приглашение' in cap2.get(agent_url))
    R.check("Каталог команд переключается в список", 'directory-list' in anon.get("/dota2/teams?view=list").text)
    R.check("Команда открывается из списка", f'/dota2/teams/{t2}' in anon.get("/dota2/teams?view=list").text)
    r = cap2.c.post(f"/dota2/agents/{uid(ag.email)}/invite", data={"role": "pos5", "message": "Го к нам"})
    R.check("Капитан отправил приглашение → 303", r.status_code == 303)
    me_ag = ag.get("/dota2/me")
    R.check("Игрок видит приглашение", "QA Команда Бета" in me_ag and "зовут на роль" in me_ag)
    p4.c.post(f"/dota2/agents/{uid(ag.email)}/invite", data={"role": "pos5"})
    R.check("Игрок без своей команды не может приглашать (приглашение одно — от капитана)",
            _invites(ag.email) == [(t2, "pending")], _invites(ag.email))
    inv = _find_id(me_ag, r"/invites/(\d+)/accept")
    ag.c.post(f"/invites/{inv}/accept")
    R.check("Принятое приглашение занимает слот", slots(t2)["pos5"]["user_id"] == uid(ag.email))
    R.check("Принявший пропал из списка свободных агентов", ag.nick not in anon.get("/dota2/agents").text)
    R.check("Игрок с командой больше не виден как свободный", anon.get(agent_url).status_code == 404)

    # ------------------------------------------------------------------ 7
    R.sec("7. Жизненный цикл команды")
    s = slots(t1)
    cap.c.post(f"/dota2/teams/{t1}/slots/{s['pos3']['id']}", data={"action": "set_open"})
    R.check("Капитан открыл занятый другом слот", slots(t1)["pos3"]["state"] == "open")
    p4.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos3"], "message": "x"})
    p5.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos5"], "message": "x"})
    me_cap = cap.get("/dota2/me")
    for aid in re.findall(r"/applications/(\d+)/accept", me_cap):
        cap.c.post(f"/applications/{aid}/accept")
    R.check("Команда собрана: 5 из 5, статус full", team_row(t1)["status"] == "full", team_row(t1))
    R.check("В списке показана как «собрана»", "собрана" in anon.get("/dota2/teams").text)
    R.check("Фильтр «только с открытыми слотами» скрывает собранную",
            "QA Команда Альфа" not in anon.get("/dota2/teams?open_only=true").text)
    late = Actor("late")
    late.register()
    late.profile("dota2", "3000", ["pos1"])
    r = late.c.post(f"/dota2/teams/{t1}/apply", data={"roles": ["pos1"]})
    R.check("В собранную команду заявку подать нельзя",
            _apps(late.email, t1) == 0 and any("не набирает" in f for f in flashes(late.c.get(loc(r)).text)),
            flashes(late.c.get(loc(r)).text))

    cap.c.post(f"/dota2/teams/{t1}/slots/{slots(t1)['pos3']['id']}", data={"action": "kick"})
    R.check("Исключённый игрок освобождает слот, команда снова в наборе",
            slots(t1)["pos3"]["state"] == "open" and team_row(t1)["status"] == "recruiting")
    R.check("Исключённый получил уведомление", "исключили" in p4.get("/notifications"))
    cap.c.post(f"/dota2/teams/{t1}/slots/{slots(t1)['pos1']['id']}", data={"action": "kick"})
    R.check("Капитана исключить нельзя", slots(t1)["pos1"]["state"] == "filled")
    p5.c.post(f"/dota2/teams/{t1}/leave")
    R.check("Участник может сам покинуть команду", slots(t1)["pos5"]["state"] == "open")
    cap.c.post(f"/dota2/teams/{t1}/leave")
    R.check("Капитан не может просто выйти", slots(t1)["pos1"]["state"] == "filled")
    cap.c.post(f"/dota2/teams/{t1}/transfer-captain", data={"new_captain_user_id": "999999"})
    R.check("Капитанство нельзя передать не-участнику", team_row(t1)["captain_id"] == uid(cap.email))
    cap.c.post(f"/dota2/teams/{t1}/transfer-captain", data={"new_captain_user_id": str(uid(p1.email))})
    R.check("Капитанство передано участнику", team_row(t1)["captain_id"] == uid(p1.email))
    r = cap.c.post(f"/dota2/teams/{t1}/disband")
    R.check("Бывший капитан не может расформировать команду", team_row(t1)["status"] != "disbanded")
    p1.c.post(f"/dota2/teams/{t1}/disband")
    R.check("Новый капитан расформировал команду", team_row(t1)["status"] == "disbanded")
    R.check("Расформированной команды нет в списке", "QA Команда Альфа" not in anon.get("/dota2/teams").text)
    R.check("Бывший участник снова может создать команду",
            create_team(p4, "dota2", "QA Команда Гамма", "pos3") is not None)

    # ------------------------------------------------------------------ 8
    R.sec("8. Админка")
    adm = Actor("admin")
    r = adm.register()
    R.check("Почта из ADMIN_EMAILS получает права админа при регистрации", _field(adm.email, "is_admin") is True)
    for url in ["/admin/dota2", "/admin/lol", "/admin/dota2/teams", "/admin/dota2/players",
                "/admin/reset-requests", "/admin/admins", "/admin/demo"]:
        R.check(f"Обычному пользователю {url} недоступен", p2.c.get(url).status_code == 303, url)
    dash = adm.get("/admin/dota2")
    R.check("Дашборд: счётчики и гистограмма рейтинга", "пользователей всего" in dash and "hist-bar" in dash)
    R.check("Дашборд: дефицит ролей", "Дефицит ролей" in dash)
    R.check("Админка LoL открывается отдельно", adm.c.get("/admin/lol").status_code == 200)
    R.check("Список команд в админке", "QA Команда Бета" in adm.get("/admin/dota2/teams"))
    R.check("Список игроков в админке", p2.nick in adm.get("/admin/dota2/players"))
    r = adm.c.get("/admin/dota2/teams", params={"q": "'; DROP TABLE users;--"})
    R.check("SQL-инъекция в поиске не ломает ничего", r.status_code == 200 and _exists(cap.email))

    cap2.c.post(f"/dota2/teams/{t2}/edit", data={"name": 'Команда, "Омега"', "tag": "QA", "description": "",
                "rating_min_wanted": "", "rating_max_wanted": ""})
    r = adm.c.get("/admin/dota2/export.csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    R.check("CSV-экспорт: корректный text/csv", r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""))
    R.check("CSV: заголовок и строки состава", len(rows) > 1 and rows[0][0] == "Команда", rows[:1])
    R.check("CSV: название с запятой и кавычками не ломает разметку",
            any(row and row[0] == 'Команда, "Омега"' for row in rows))

    # сброс доступа через администратора
    cl = Actor("cantlogin")
    cl.register()
    new_client().post("/auth/cant-login", data={"identifier": cl.email, "contact_note": "пишите в тг"})
    page = adm.get("/admin/reset-requests")
    R.check("Заявка «не могу войти» попала в очередь админа", cl.email in page)
    adm.c.post(f"/admin/users/{uid(cl.email)}/issue-reset-link", data={"request_id": ""})
    page = adm.get("/admin/reset-requests")
    tok = (re.search(r"/auth/reset/([A-Za-z0-9_-]+)", page) or [None, None])[1]
    R.check("Админ получил одноразовую ссылку для входа", bool(tok))
    rc = new_client()
    R.check("Страница сброса по ссылке открывается", rc.get(f"/auth/reset/{tok}").status_code == 200)
    r = rc.post(f"/auth/reset/{tok}", data={"new_password": "reset-pass-3", "new_password2": "reset-pass-3"})
    R.check("Новый пароль задан по ссылке → 303", r.status_code == 303)
    R.check("Вход по новому паролю работает", new_client().post(
        "/auth/login", data={"email": cl.email, "password": "reset-pass-3", "next": "/"}).status_code == 303)
    r = new_client().get(f"/auth/reset/{tok}")
    R.check("Ссылка одноразовая: повторное использование отклоняется",
            r.status_code == 303 and "cant-login" in loc(r), loc(r))

    # управление админами
    r = adm.c.post(f"/admin/users/{uid(adm.email)}/toggle-admin")
    R.check("Админ не может снять права с самого себя", _field(adm.email, "is_admin") is True)
    adm.c.post(f"/admin/users/{uid(p3.email)}/toggle-admin")
    R.check("Права админа выданы другому пользователю", p3.c.get("/admin/dota2").status_code == 200)
    adm.c.post(f"/admin/users/{uid(p3.email)}/toggle-admin")
    R.check("Права сняты — доступ закрыт сразу", p3.c.get("/admin/dota2").status_code == 303)

    # бан
    pid = profile_id(p5.email, "dota2")
    adm.c.post(f"/admin/dota2/players/{pid}/ban")
    R.check("Заблокированный игрок не может войти", p5.login().status_code == 400)
    R.check("Сессия заблокированного перестаёт работать", p5.c.get("/dota2/me").status_code == 303)
    adm.c.post(f"/admin/dota2/players/{pid}/ban")
    R.check("После разблокировки вход возвращается", p5.login().status_code == 303)
    r = adm.c.post(f"/admin/dota2/players/{pid}/reverify")
    R.check("Перепроверка профиля без Steam ID корректно отвечает (без падения)", r.status_code == 303)

    # ------------------------------------------------------------------ 9
    R.sec("9. Демо-режим и удаление тестовых данных")
    page = adm.get("/admin/demo")
    R.check("Страница демо-режима открывается админу", "Демо-режим" in page)
    R.check("Страница скрыта из навигации админки",
            "/admin/demo" not in adm.get("/admin/dota2") and "/admin/demo" not in adm.get("/admin/admins"))
    before = _count(User)
    r = adm.c.post("/admin/demo/seed")
    R.check("Наполнение демо-данными через админку → 303", r.status_code == 303, r.status_code)
    with SessionLocal() as s_:
        from app.demo.purge import demo_counts
        counts = demo_counts(s_)
    R.check("Создано больше 50 игроков и 10 команд", counts["users_total"] > 50 and counts["teams_total"] > 10, counts)
    r = adm.c.post("/admin/demo/seed")
    R.check("Повторное наполнение не дублирует данные",
            demo_counts_total() == counts["users_total"] + counts["teams_total"], demo_counts_total())

    public = anon.get("/dota2/teams").text
    R.check("Демо-команды видны посетителям в списке команд", public.count('class="card team-card"') > 8)
    R.check("Демо-игроки видны в свободных игроках", anon.get("/dota2/agents").text.count('class="card player-card directory-card"') > 5)
    lower = (public + anon.get("/dota2/agents").text + anon.get("/lol/teams").text).lower()
    R.check("Служебная метка демо-данных не просачивается в публичный HTML",
            not any(w in lower for w in ["demo_tag", "seed", "демо", "тестов"]))
    R.check("Демо-аккаунты нельзя залогинить паролем", new_client().post(
        "/auth/login", data={"email": "user001@mail.invalid", "password": PASSWORD, "next": "/"}).status_code == 400)

    # вход под демо-персонажем
    page = adm.get("/admin/demo")
    persona = re.search(r'action="/admin/demo/impersonate/(\d+)"', page)
    R.check("На странице есть демо-персонажи для входа", bool(persona))
    r = adm.c.post(f"/admin/demo/impersonate/{uid(cap.email)}", data={"game": "dota2"})
    R.check("Под настоящим пользователем зайти нельзя (404)", r.status_code == 404, r.status_code)
    r = adm.c.post(f"/admin/demo/impersonate/{persona.group(1)}", data={"game": "dota2"})
    R.check("Вход под демо-персонажем → 303 в кабинет", r.status_code == 303 and "/me" in loc(r), loc(r))
    R.check("Кабинет персонажа открывается", adm.c.get("/dota2/me").status_code == 200)
    R.check("Под персонажем админка недоступна (он не админ)", adm.c.get("/admin/dota2").status_code == 303)
    r = adm.c.post("/auth/logout")
    R.check("«Выйти» возвращает администратора в админку", r.status_code == 303 and "/admin/demo" in loc(r), loc(r))
    R.check("После возврата админка снова доступна", adm.c.get("/admin/dota2").status_code == 200)

    # настоящий человек подал заявку в демо-команду, затем демо-данные удаляются
    with SessionLocal() as s_:
        dt = (s_.query(Team).filter(Team.demo_tag == "seed", Team.game == "dota2", Team.status == "recruiting").all())
        target = next((t for t in dt if any(x.state == "open" for x in t.slots)), None)
        open_role = next(x.role for x in target.slots if x.state == "open")
        target_id = target.id
    late = Actor("late")  # уже зарегистрирован выше, без команды
    late.login()
    r = late.c.post(f"/dota2/teams/{target_id}/apply", data={"roles": [open_role], "message": "реальная заявка"})
    R.check("Настоящий игрок может подать заявку в демо-команду", _apps(late.email, target_id) == 1)

    r = adm.c.post("/admin/demo/purge")
    R.check("Удаление демо-данных через админку → 303", r.status_code == 303, r.status_code)
    R.check("Число пользователей вернулось к исходному", _count(User) == before, f"{before}→{_count(User)}")
    R.check("Демо-меток в базе не осталось", demo_counts_total() == 0, demo_counts_total())
    R.check("Заявка настоящего игрока в демо-команду удалена вместе с ней", _apps(late.email, target_id) == 0)
    R.check("Сам настоящий игрок и его кабинет целы", _exists(late.email) and late.c.get("/dota2/me").status_code == 200)
    R.check("Настоящие команды на месте", team_row(t2)["status"] == "recruiting" and
            team_row(t_lol)["status"] in ("recruiting", "full"))
    r = adm.c.post("/admin/demo/purge")
    R.check("Повторное удаление безопасно (ничего не ломает)", r.status_code == 303 and _exists(cap.email))

    return R.steps


def _exists(email: str) -> bool:
    with SessionLocal() as s:
        return s.query(User).filter(User.email == email).count() > 0


def _field(email: str, name: str):
    with SessionLocal() as s:
        u = s.query(User).filter(User.email == email).first()
        return getattr(u, name) if u else None


def _apps(email: str, team_id: int) -> int:
    from app.models import Application
    with SessionLocal() as s:
        return s.query(Application).filter(Application.user_id == uid(email), Application.team_id == team_id).count()


def _invites(email: str) -> list:
    from app.models import Invite
    with SessionLocal() as s:
        return [(i.team_id, i.status) for i in s.query(Invite).filter(Invite.user_id == uid(email))]


def _count(model) -> int:
    with SessionLocal() as s_:
        return s_.query(model).count()


def demo_counts_total() -> int:
    from app.demo.purge import demo_counts
    with SessionLocal() as s_:
        c = demo_counts(s_)
    return c["users_total"] + c["teams_total"]


def _find_id(html: str, pattern: str) -> int | None:
    m = re.search(pattern, html)
    return int(m.group(1)) if m else None


def _app_status(app_id: int | None) -> str | None:
    from app.models import Application
    if app_id is None:
        return None
    with SessionLocal() as s:
        a = s.get(Application, app_id)
        return a.status if a else None


def main() -> int:
    t0 = time.time()
    steps: list[Step] = []
    error = None
    try:
        steps = run()
    except Exception as e:  # падение сценария = тоже результат, а не «всё ок»
        import traceback
        error = f"{type(e).__name__}: {e}"
        steps = R.steps + [Step(R.section or "—", "Сценарий аварийно завершился", False, traceback.format_exc()[-600:])]
    passed = sum(1 for s in steps if s.ok)
    report = {"steps": [asdict(s) for s in steps], "total": len(steps), "passed": passed,
              "failed": len(steps) - passed, "seconds": round(time.time() - t0, 1), "error": error}
    print("@@RESULT@@" + json.dumps(report, ensure_ascii=False))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
