from app.games.base import GameConfig, RatingChoice, RoleDef

ROLES = [
    RoleDef("top", "Топ", "TOP"),
    RoleDef("jungle", "Лес", "JG"),
    RoleDef("mid", "Мид", "MID"),
    RoleDef("adc", "АДК (бот)", "ADC"),
    RoleDef("support", "Саппорт", "SUP"),
]

_DIVIDED_TIERS = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum", "Emerald", "Diamond",
]
_APEX_TIERS = ["Master", "Grandmaster", "Challenger"]
_DIVISIONS = ["IV", "III", "II", "I"]  # от младшего к старшему


def _build_rating_choices() -> list[RatingChoice]:
    choices: list[RatingChoice] = []
    ordinal = 0
    for tier in _DIVIDED_TIERS:
        for div in _DIVISIONS:
            key = f"{tier.lower()}_{div.lower()}"
            choices.append(RatingChoice(key=key, label=f"{tier} {div}", ordinal=ordinal, major_label=tier))
            ordinal += 1
    for tier in _APEX_TIERS:
        key = tier.lower()
        choices.append(RatingChoice(key=key, label=tier, ordinal=ordinal, major_label=tier))
        ordinal += 1
    return choices


RATING_CHOICES = _build_rating_choices()

REGIONS = ["EUNE", "EUW", "RU", "TR", "Другой"]

LOL = GameConfig(
    key="lol",
    label="League of Legends",
    roster_size=5,
    roles=ROLES,
    rating_kind="tier",
    rating_choices=RATING_CHOICES,
    region_choices=REGIONS,
    account_id_label="Riot ID (Ник#TAG)",
    account_id_placeholder="Nickname#EUNE",
    profile_url_label="Ссылка на профиль",
    profile_url_placeholder="https://op.gg/summoners/eune/Nickname-EUNE",
    profile_url_hint="op.gg или u.gg — автопроверки нет, админ смотрит вручную",
    has_auto_verify=False,
    verify_fn=None,
)
