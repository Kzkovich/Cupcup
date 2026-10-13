"""Демо-наполнение: правдоподобные команды, игроки, заявки и приглашения.

Все записи помечаются User.demo_tag / Team.demo_tag = "seed" — по этой метке
purge_demo() удаляет их одним вызовом. В интерфейсе метка нигде не показывается.
Демо-аккаунты нельзя залогинить паролем (пароль случайный, почта на .invalid) —
зайти под ними можно только из админки (раздел /admin/demo).
"""
import random
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.games import GAMES
from app.games.base import GameConfig
from app.games.dota2 import _MEDALS
from app.models import Application, GameProfile, Invite, Team, TeamSlot, User
from app.notify import notify
from app.security import hash_password

SEED_TAG = "seed"

_FIRST = [
    "Kirill", "Dmitry", "Alex", "Nikita", "Sergey", "Maksim", "Ilya", "Artem", "Pavel", "Vlad",
    "Andrey", "Roman", "Egor", "Denis", "Timur", "Oleg", "Stas", "Yura", "Gleb", "Misha",
    "Anton", "Boris", "Vitaly", "Zhenya", "Lev", "Matvey", "Fedor", "Danila", "Kostya", "Ruslan",
    "Igor", "Ivan", "Vadim", "Semyon", "Arseny", "Platon", "Tikhon", "Eldar", "Marat", "Grisha",
]
_SUFFIX = [
    "Pro", "Fox", "Wolf", "Dark", "Storm", "Void", "Nova", "Mid", "Carry", "Sup", "Tank",
    "Ace", "Zed", "X", "93", "7", "King", "Bear", "Hawk", "Frost", "Blade", "Rush", "Ghost",
]

_DESCRIPTIONS = [
    "Играем по вечерам в будни с 20:00, по выходным — тренировки. Ищем активных, без токсичности.",
    "Собираемся на турнир, цель — выйти из группы. Нужен дисциплинированный состав.",
    "Команда коллег, ищем недостающих игроков. Опыт приветствуется, но главное — желание играть.",
    "Играем для удовольствия, но к турниру подходим серьёзно. Скримы раз в неделю.",
    "Есть опыт совместных игр, не хватает пары ролей. Готовы к тренировкам.",
    "Ищем активных и задротов. Пишите, если готовы играть каждый вечер.",
    "Состав почти собран, ищем последних. Важна коммуникация в голосовом чате.",
    "Новая команда, играем спокойно, без нервов. Рейтинг не главное.",
]
_APPLY_MSGS = [
    "Играю по вечерам, готов на тренировки.",
    "Есть опыт на этой позиции, на связи в Discord.",
    "Могу брать любую роль по необходимости.",
    "Давно ищу команду для турнира, готов к скримам.",
    "Уверенно играю, коммуникация без токсичности.",
    "Свободен по будням после 19:00 и в выходные.",
]
_INVITE_MSGS = [
    "Увидели твой профиль — нам как раз нужна эта роль. Присоединяйся!",
    "Ищем игрока на эту позицию, рейтинг подходит. Давай к нам?",
    "Собираем состав на турнир, ты нам подходишь по роли и уровню.",
]
_CONTACTS = [
    "Telegram — ник как на сайте",
    "Лучше всего в рабочем чате",
    "Discord, ник как на сайте",
    None, None,
]

_TEAM_NAMES = {
    "dota2": [
        "Овердрафт", "Кэшбэк Кингз", "Холодный Расчёт", "Процентная Ставка", "Альфа Рейд",
        "Ликвидность", "Транш", "Хардлайн Банк", "Скоринг", "Рублёвый Эшелон",
        "Дебет и Кредит", "Мидлайн Капитал",
    ],
    "lol": [
        "Красная Линия", "Лимит Доверия", "Синергия Банк", "Ночной Овернайт", "Фулл Депозит",
        "Ставка Рефинансирования", "Бонусный Раунд", "Тихий Андеррайтинг", "Кросс-Курс",
    ],
}
_TEAM_TAGS = {
    "dota2": ["OVD", "KBK", "HRS", "PRS", "ALF", "LIQ", "TRN", "HLB", "SCR", "RUB", "DBK", "MLC"],
    "lol": ["KRL", "LTD", "SNB", "NOV", "FDP", "REF", "BNS", "UWR", "XRT"],
}

# сколько команд каждого статуса и сколько свободных агентов в каждой дисциплине
_PLAN = {
    "dota2": {"full": 4, "recruiting": 6, "locked": 2, "agents": 20},
    "lol": {"full": 3, "recruiting": 5, "locked": 1, "agents": 14},
}
# популярность ролей у ищущих игроков (на кор-ролях очередь, саппортов не хватает)
_ROLE_POPULARITY = {
    "dota2": {"pos1": 1.0, "pos2": 0.9, "pos3": 0.65, "pos4": 0.4, "pos5": 0.3},
    "lol": {"mid": 1.0, "adc": 0.85, "top": 0.7, "jungle": 0.45, "support": 0.35},
}


class AlreadySeeded(Exception):
    pass


class _Names:
    def __init__(self, rng: random.Random):
        combos = {f"{n}_{s}" for n in _FIRST for s in _SUFFIX}
        combos |= {f"{n}{s}" for n in _FIRST for s in _SUFFIX}
        self._pool = sorted(combos)
        rng.shuffle(self._pool)
        self._i = 0

    def next(self) -> str:
        nick = self._pool[self._i]
        self._i += 1
        return nick


def _ago(rng: random.Random, now: datetime, min_h: float, max_h: float) -> datetime:
    return now - timedelta(hours=rng.uniform(min_h, max_h))


def _rate(game: GameConfig, rng: random.Random, base: float | None = None, spread: float = 1.0):
    """(rating_raw, ordinal). base — «уровень команды», чтобы составы были однородными."""
    if game.rating_kind == "numeric":
        centre = base if base is not None else rng.gauss(3900, 1250)
        mmr = int(round(rng.gauss(centre, 380 * spread) / 10.0) * 10)
        mmr = max(600, min(9500, mmr))
        return str(mmr), mmr
    top = len(game.rating_choices) - 1
    centre = base if base is not None else rng.gauss(13, 5.5)
    idx = int(round(rng.gauss(centre, 2.3 * spread)))
    idx = max(0, min(top, idx))
    c = game.rating_choices[idx]
    return c.key, c.ordinal


def _base_level(game: GameConfig, rng: random.Random) -> float:
    return rng.gauss(3900, 1100) if game.rating_kind == "numeric" else rng.gauss(13, 5)


def _dota_verification(mmr: int, rng: random.Random):
    """Правдоподобный статус автопроверки для профиля Dota."""
    roll = rng.random()
    medal = min(8, mmr // 770 + 1)
    star = min(5, (mmr % 770) // 154 + 1)
    label = "Immortal" if medal == 8 else f"{_MEDALS[medal]} {star}"
    if roll < 0.46:
        return "ok", str(mmr), label
    if roll < 0.54:
        low_medal = max(1, medal - 2)
        return "mismatch", str(max(300, mmr - 1700)), f"{_MEDALS[low_medal]} {star}"
    if roll < 0.62:
        return "private", None, None
    if roll < 0.66:
        return "failed", None, None
    return "none", None, None


def _slug(nick: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", nick.lower())


def seed_demo(db: Session, seed: int = 7) -> dict:
    """Наполняет базу. Бросает AlreadySeeded, если демо-данные уже есть."""
    if db.query(User).filter(User.demo_tag == SEED_TAG).count():
        raise AlreadySeeded("Демо-данные уже созданы — сначала удалите их")

    rng = random.Random(seed)
    now = datetime.utcnow()
    names = _Names(rng)
    pwd_hash = hash_password(secrets.token_urlsafe(32))  # один хэш на всех: войти по нему нельзя
    counter = {"n": 0}

    def make_user(created: datetime) -> User:
        counter["n"] += 1
        u = User(
            email=f"user{counter['n']:03d}@mail.invalid",
            password_hash=pwd_hash,
            contact_note=rng.choice(_CONTACTS),
            demo_tag=SEED_TAG,
            created_at=created,
            last_seen_at=created + (now - created) * rng.random(),
        )
        db.add(u)
        db.flush()
        return u

    def make_profile(user: User, game: GameConfig, nick: str, roles: list[str],
                     base: float | None, looking: bool, created: datetime) -> GameProfile:
        raw, ordinal = _rate(game, rng, base)
        status, v_raw, v_label, v_at = "none", None, None, None
        if game.key == "dota2":
            status, v_raw, v_label = _dota_verification(int(raw), rng)
            if status != "none":
                v_at = created + (now - created) * rng.random()
        p = GameProfile(
            user_id=user.id, game=game.key, nickname=nick,
            region=rng.choices(["EUNE", "EUW", "RU", "TR"], [4, 3, 2, 1])[0] if game.region_choices else None,
            account_id=f"{nick}#EUNE" if game.key == "lol" else None,
            rating_raw=raw, rating_ordinal=ordinal,
            verify_status=status, verified_raw=v_raw, verified_label=v_label, verified_at=v_at,
            preferred_roles=roles, is_looking=looking,
            about=rng.choice([None, None, "Играю вечерами, на связи в Discord.",
                              "Ищу команду на турнир, готов к тренировкам.",
                              "Опыт командных игр есть, без токсичности."]),
            created_at=created, updated_at=created + (now - created) * rng.random(),
        )
        db.add(p)
        db.flush()
        return p

    def agent_roles(game: GameConfig) -> list[str]:
        pop = _ROLE_POPULARITY[game.key]
        keys = [r.key for r in game.roles]
        first = rng.choices(keys, [pop[k] for k in keys])[0]
        roles = [first]
        if rng.random() < 0.45:
            second = rng.choices([k for k in keys if k != first], [pop[k] for k in keys if k != first])[0]
            roles.append(second)
        return roles

    def add_notif(user_id: int, type_: str, when: datetime, read: bool, **payload) -> None:
        n = notify(db, user_id, type_, **payload)
        n.created_at = when
        n.read_at = (when + timedelta(minutes=rng.randint(3, 600))) if read else None

    for game_key in ("dota2", "lol"):
        game = GAMES[game_key]
        plan = _PLAN[game_key]
        team_names = list(_TEAM_NAMES[game_key])
        tags = list(_TEAM_TAGS[game_key])
        rng.shuffle(team_names)
        statuses = (["full"] * plan["full"] + ["recruiting"] * plan["recruiting"] + ["locked"] * plan["locked"])
        rng.shuffle(statuses)

        teams: list[Team] = []
        members_by_team: dict[int, list[tuple[User, GameProfile, str]]] = {}
        all_roles = [r.key for r in game.roles]

        for i, status in enumerate(statuses):
            captain_created = _ago(rng, now, 24, 24 * 12)
            base = _base_level(game, rng)
            if status == "full":
                n_members = 5
            elif status == "recruiting":
                n_members = rng.choice([1, 2, 2, 3, 3, 4])
            else:
                n_members = rng.choice([2, 3, 4])

            role_order = all_roles[:]
            rng.shuffle(role_order)
            member_roles = role_order[:n_members]
            rest = role_order[n_members:]
            reserved_roles = []
            if status != "full" and len(rest) >= 2 and rng.random() < 0.4:
                reserved_roles = [rest[0]]
            open_roles = [r for r in rest if r not in reserved_roles]

            team_members: list[tuple[User, GameProfile, str]] = []
            for j, role in enumerate(member_roles):
                created = captain_created + timedelta(hours=rng.uniform(1, 70)) if j else captain_created
                created = min(created, now - timedelta(hours=1))
                u = make_user(created)
                nick = names.next()
                extra = [r for r in all_roles if r != role]
                roles = [role] + ([rng.choice(extra)] if rng.random() < 0.3 else [])
                p = make_profile(u, game, nick, roles, base, False, created)
                team_members.append((u, p, role))

            captain_user, captain_profile, _ = team_members[0]
            ords = [p.rating_ordinal for _, p, _ in team_members]
            avg = sum(ords) / len(ords)
            rmin = rmax = None
            if rng.random() < 0.55:
                if game.rating_kind == "numeric":
                    rmin, rmax = int(avg - rng.randint(700, 1400)), int(avg + rng.randint(700, 1400))
                    rmin, rmax = max(0, rmin), min(12000, rmax)
                else:
                    top = len(game.rating_choices) - 1
                    rmin = max(0, int(avg - rng.randint(3, 6)))
                    rmax = min(top, int(avg + rng.randint(3, 6)))

            created_at = captain_user.created_at + timedelta(hours=rng.uniform(0.5, 6))
            created_at = min(created_at, now - timedelta(hours=1))
            bumped = created_at + (now - created_at) * rng.random() ** 2
            if status == "recruiting" and rng.random() < 0.5:
                bumped = _ago(rng, now, 0.2, 9)   # свежий «поднятый» набор
            team = Team(
                game=game_key, name=team_names[i], tag=tags[i], captain_id=captain_user.id,
                description=rng.choice(_DESCRIPTIONS), status=status,
                rating_min_wanted=rmin, rating_max_wanted=rmax,
                created_at=created_at, bumped_at=bumped, demo_tag=SEED_TAG,
            )
            db.add(team)
            db.flush()

            slot_by_role = {}
            for role in all_roles:
                slot = TeamSlot(team_id=team.id, role=role, state="open")
                db.add(slot)
                slot_by_role[role] = slot
            for u, p, role in team_members:
                s = slot_by_role[role]
                s.state, s.user_id = "filled", u.id
            for role in reserved_roles:
                s = slot_by_role[role]
                s.state, s.note = "reserved", rng.choice(["друг подтянется позже", "ждём коллегу из соседнего отдела",
                                                           "место занято, игрок вернётся из отпуска"])
            db.flush()

            teams.append(team)
            members_by_team[team.id] = team_members

        # --- свободные агенты ---
        agents: list[tuple[User, GameProfile]] = []
        n_shared = 0
        if game_key == "lol":
            # часть игроков играет в обе дисциплины: у них уже есть аккаунт в Dota,
            # добавляем им профиль LoL и они становятся агентами
            dota_free = (
                db.query(User).join(GameProfile, GameProfile.user_id == User.id)
                .filter(User.demo_tag == SEED_TAG, GameProfile.game == "dota2", GameProfile.is_looking.is_(True))
                .limit(5).all()
            )
            for u in dota_free:
                nick = db.query(GameProfile).filter(GameProfile.user_id == u.id).first().nickname
                p = make_profile(u, game, nick, agent_roles(game), None, True, _ago(rng, now, 6, 24 * 5))
                agents.append((u, p))
                n_shared += 1

        for _ in range(plan["agents"] - n_shared):
            created = _ago(rng, now, 1, 24 * 9)
            u = make_user(created)
            looking = rng.random() < 0.94
            p = make_profile(u, game, names.next(), agent_roles(game), None, looking, created)
            agents.append((u, p))

        looking_agents = [(u, p) for u, p in agents if p.is_looking]

        # --- заявки (ожидают решения) ---
        recruiting = [t for t in teams if t.status == "recruiting"]
        applied: set[tuple[int, int]] = set()
        for t in recruiting:
            open_roles_t = [s.role for s in db.query(TeamSlot).filter(TeamSlot.team_id == t.id, TeamSlot.state == "open")]
            if not open_roles_t or rng.random() > 0.8:
                continue
            for _ in range(rng.choice([1, 2, 2, 3])):
                pool = [
                    (u, p) for u, p in looking_agents
                    if (u.id, t.id) not in applied and set(p.preferred_roles) & set(open_roles_t)
                ] or [(u, p) for u, p in looking_agents if (u.id, t.id) not in applied]
                if not pool:
                    break
                u, p = rng.choice(pool)
                roles = [r for r in p.preferred_roles if r in open_roles_t] or [rng.choice(open_roles_t)]
                when = _ago(rng, now, 0.3, 60)
                db.add(Application(team_id=t.id, user_id=u.id, roles=roles[:2],
                                   message=rng.choice(_APPLY_MSGS), status="pending", created_at=when))
                applied.add((u.id, t.id))
                add_notif(t.captain_id, "application_received", when, rng.random() < 0.3,
                          team_id=t.id, team_name=t.name, applicant=p.nickname, game=game_key)

        # --- история: принятые заявки, отказы, отзывы ---
        for t in teams:
            for u, p, role in members_by_team[t.id][1:]:
                if rng.random() < 0.65:
                    sent = max(u.created_at + timedelta(hours=1), t.created_at)
                    sent = min(sent, now - timedelta(hours=2))
                    decided = sent + timedelta(hours=rng.uniform(0.5, 20))
                    db.add(Application(team_id=t.id, user_id=u.id, roles=[role], status="accepted",
                                       message=rng.choice(_APPLY_MSGS), created_at=sent,
                                       decided_at=min(decided, now), decided_by=t.captain_id))
                    add_notif(u.id, "application_accepted", min(decided, now), True,
                              team_id=t.id, team_name=t.name, role=role, game=game_key)

        for _ in range(6):
            if not agents or not recruiting:
                break
            (u, p), t = rng.choice(agents), rng.choice(recruiting)
            if (u.id, t.id) in applied:
                continue
            applied.add((u.id, t.id))
            sent = _ago(rng, now, 20, 24 * 6)
            status = rng.choice(["rejected", "rejected", "rejected", "withdrawn"])
            db.add(Application(team_id=t.id, user_id=u.id, roles=[rng.choice([r.key for r in game.roles])],
                               message=rng.choice(_APPLY_MSGS), status=status, created_at=sent,
                               decided_at=sent + timedelta(hours=rng.uniform(1, 18)),
                               decided_by=t.captain_id if status == "rejected" else None))
            if status == "rejected":
                add_notif(u.id, "application_rejected", sent + timedelta(hours=3), True,
                          team_id=t.id, team_name=t.name, game=game_key)

        # --- приглашения ---
        invitable = [t for t in recruiting if any(s.state == "open" for s in t.slots)] or recruiting
        for k in range(min(5, len(invitable))):
            t = invitable[k]
            open_roles_t = [s.role for s in db.query(TeamSlot).filter(TeamSlot.team_id == t.id, TeamSlot.state == "open")]
            pool = [(u, p) for u, p in looking_agents
                    if set(p.preferred_roles) & set(open_roles_t) and (u.id, t.id) not in applied]
            if not pool or not open_roles_t:
                continue
            u, p = rng.choice(pool)
            role = next((r for r in p.preferred_roles if r in open_roles_t), open_roles_t[0])
            when = _ago(rng, now, 0.5, 40)
            declined = k >= 3
            db.add(Invite(team_id=t.id, user_id=u.id, role=role, message=rng.choice(_INVITE_MSGS),
                          status="declined" if declined else "pending", created_at=when,
                          decided_at=when + timedelta(hours=4) if declined else None))
            applied.add((u.id, t.id))
            add_notif(u.id, "invite_received", when, declined, team_id=t.id, team_name=t.name,
                      role=role, game=game_key)
            if declined:
                add_notif(t.captain_id, "invite_declined", when + timedelta(hours=4), True,
                          team_id=t.id, team_name=t.name, game=game_key)

        db.flush()

    db.commit()

    return {
        "users": db.query(User).filter(User.demo_tag == SEED_TAG).count(),
        "teams": db.query(Team).filter(Team.demo_tag == SEED_TAG).count(),
        "profiles": db.query(GameProfile).join(User, User.id == GameProfile.user_id)
                      .filter(User.demo_tag == SEED_TAG).count(),
        "applications": db.query(Application).join(User, User.id == Application.user_id)
                          .filter(User.demo_tag == SEED_TAG).count(),
        "invites": db.query(Invite).join(User, User.id == Invite.user_id)
                     .filter(User.demo_tag == SEED_TAG).count(),
    }
