import re

import httpx

from app.games.base import GameConfig, RoleDef, VerifyResult

ROLES = [
    RoleDef("pos1", "Керри (1)", "1"),
    RoleDef("pos2", "Мидер (2)", "2"),
    RoleDef("pos3", "Хардлайнер (3)", "3"),
    RoleDef("pos4", "Саппорт-роумер (4)", "4"),
    RoleDef("pos5", "Фулл-саппорт (5)", "5"),
]

# Медали OpenDota rank_tier: первая цифра — медаль (1..8), вторая — звезда (1..5).
# 80 = Immortal, leaderboard-позиция приходит отдельным полем.
_MEDALS = {
    1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
    5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal",
}

# Грубое сопоставление медаль+звезда -> ориентировочный MMR (середина диапазона)
# для сверки с указанным игроком числом. Используется только для проверки
# правдоподобия, не как точный расчёт.
def _medal_to_mmr(rank_tier: int) -> int | None:
    if not rank_tier:
        return None
    medal, star = divmod(rank_tier, 10)
    if medal == 8:
        return 5620  # Immortal — дальше решает leaderboard_rank
    base = (medal - 1) * 770
    return base + (star - 1) * 154 + 77


def _extract_account_id(raw: str) -> str | None:
    raw = raw.strip()
    if raw.isdigit():
        return raw
    m = re.search(r"(?:dotabuff\.com/players/|opendota\.com/players/|stratz\.com/players/)(\d+)", raw)
    if m:
        return m.group(1)
    return None


def verify_dota_profile(account_id_or_url: str, claimed_mmr: str) -> VerifyResult:
    account_id = _extract_account_id(account_id_or_url)
    if not account_id:
        return VerifyResult(status="failed", detail="Не удалось распознать Steam ID / ссылку")
    try:
        resp = httpx.get(f"https://api.opendota.com/api/players/{account_id}", timeout=8.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return VerifyResult(status="failed", detail=f"OpenDota недоступен: {e}")

    if data.get("profile") is None:
        return VerifyResult(status="failed", detail="Профиль не найден в OpenDota")

    rank_tier = data.get("rank_tier")
    leaderboard_rank = data.get("leaderboard_rank")

    if not rank_tier:
        return VerifyResult(status="private", detail="Профиль скрыт или без соревновательного рейтинга")

    medal, star = divmod(rank_tier, 10)
    medal_label = _MEDALS.get(medal, "Неизвестно")
    label = f"{medal_label} {star}"
    if medal == 8 and leaderboard_rank:
        label = f"Immortal #{leaderboard_rank}"

    est_mmr = _medal_to_mmr(rank_tier)
    try:
        claimed = int(claimed_mmr)
    except (TypeError, ValueError):
        claimed = None

    status = "ok"
    detail = None
    if est_mmr is not None and claimed is not None:
        if abs(claimed - est_mmr) > 1200:
            status = "mismatch"
            detail = f"Заявлено {claimed} MMR, по медали похоже на ~{est_mmr} MMR ({label})"

    return VerifyResult(
        status=status,
        verified_raw=str(est_mmr) if est_mmr else None,
        verified_label=label,
        detail=detail,
    )


DOTA2 = GameConfig(
    key="dota2",
    label="Dota 2",
    roster_size=5,
    roles=ROLES,
    rating_kind="numeric",
    rating_min=0,
    rating_max=12000,
    rating_step=1000,
    account_id_label="Steam ID / ссылка на Dotabuff",
    account_id_placeholder="76561198012345678 или dotabuff.com/players/12345678",
    profile_url_label="Ссылка на профиль",
    profile_url_placeholder="https://www.dotabuff.com/players/12345678",
    profile_url_hint="Dotabuff, OpenDota или Stratz — что удобнее",
    has_auto_verify=True,
    verify_fn=verify_dota_profile,
)
