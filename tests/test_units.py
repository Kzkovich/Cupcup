"""Быстрые проверки без БД: конфиги игр, рейтинги, коды восстановления."""
import re

import pytest

from app.games import GAMES
from app.security import generate_recovery_code, normalize_recovery_code


def test_both_games_registered_with_five_roles():
    assert set(GAMES) == {"dota2", "lol"}
    assert all(len(g.roles) == g.roster_size == 5 for g in GAMES.values())


def test_dota_rating_is_clamped_to_range():
    dota = GAMES["dota2"]
    assert dota.parse_rating("99999") == ("12000", 12000)
    assert dota.parse_rating("-50") == ("0", 0)


def test_lol_rating_ordinals_are_strictly_increasing_and_labelled():
    lol = GAMES["lol"]
    ords = [c.ordinal for c in lol.rating_choices]
    assert ords == sorted(set(ords)) and len(ords) == 31
    assert lol.rating_choice_by_key("iron_iv").ordinal == 0
    assert lol.display_rating("gold_ii") == "Gold II"
    with pytest.raises(ValueError):
        lol.parse_rating("not_a_tier")


def test_rating_domains_are_not_mixed_between_games():
    assert GAMES["dota2"].rating_kind == "numeric" and GAMES["lol"].rating_kind == "tier"


def test_histogram_buckets_cover_values():
    dota = GAMES["dota2"]
    buckets = dict(dota.histogram_buckets([500, 1200, 1300, 4200]))
    assert buckets["0-999"] == 1 and buckets["1000-1999"] == 2 and buckets["4000-4999"] == 1


def test_recovery_code_format_and_normalisation():
    code = generate_recovery_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert normalize_recovery_code(code.lower().replace("-", " ")) == code.replace("-", "")
