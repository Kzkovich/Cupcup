from dataclasses import dataclass, field
from typing import Callable, Literal, Optional


@dataclass(frozen=True)
class RoleDef:
    key: str
    label: str
    short: str


@dataclass(frozen=True)
class RatingChoice:
    key: str
    label: str
    ordinal: int
    major_label: str


@dataclass(frozen=True)
class VerifyResult:
    status: str  # ok | mismatch | private | failed
    verified_raw: Optional[str] = None
    verified_label: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class GameConfig:
    key: str
    label: str
    roster_size: int
    roles: list[RoleDef]
    rating_kind: Literal["numeric", "tier"]

    account_id_label: str
    account_id_placeholder: str
    profile_url_label: str
    profile_url_placeholder: str
    profile_url_hint: str

    rating_min: Optional[int] = None
    rating_max: Optional[int] = None
    rating_step: Optional[int] = None
    rating_choices: list[RatingChoice] = field(default_factory=list)
    region_choices: list[str] = field(default_factory=list)

    has_auto_verify: bool = False
    verify_fn: Optional[Callable[[str, str], VerifyResult]] = None

    def role_by_key(self, key: str) -> Optional[RoleDef]:
        for r in self.roles:
            if r.key == key:
                return r
        return None

    def rating_choice_by_key(self, key: str) -> Optional[RatingChoice]:
        for c in self.rating_choices:
            if c.key == key:
                return c
        return None

    def parse_rating(self, raw_form_value: str) -> tuple[str, int]:
        """Возвращает (значение_для_хранения, ordinal_для_сортировки)."""
        if self.rating_kind == "numeric":
            value = int(str(raw_form_value).strip())
            if self.rating_min is not None:
                value = max(self.rating_min, value)
            if self.rating_max is not None:
                value = min(self.rating_max, value)
            return str(value), value
        choice = self.rating_choice_by_key(raw_form_value)
        if choice is None:
            raise ValueError("Неизвестное значение рейтинга")
        return choice.key, choice.ordinal

    def display_rating(self, raw: Optional[str]) -> str:
        if raw is None or raw == "":
            return "—"
        if self.rating_kind == "numeric":
            return f"{raw} MMR"
        choice = self.rating_choice_by_key(raw)
        return choice.label if choice else raw

    def display_ordinal(self, ordinal: Optional[int]) -> str:
        """Показывает значение, хранящееся как ordinal (например, team.rating_*_wanted)."""
        if ordinal is None:
            return "—"
        if self.rating_kind == "numeric":
            return f"{ordinal} MMR"
        if not self.rating_choices:
            return str(ordinal)
        nearest = min(self.rating_choices, key=lambda c: abs(c.ordinal - ordinal))
        return nearest.label

    def ordinal_domain(self) -> tuple[int, int]:
        if self.rating_kind == "numeric":
            return self.rating_min or 0, self.rating_max or 0
        if not self.rating_choices:
            return 0, 0
        return self.rating_choices[0].ordinal, self.rating_choices[-1].ordinal

    def histogram_buckets(self, ordinals: list[int]) -> list[tuple[str, int]]:
        if self.rating_kind == "numeric":
            lo, hi = self.ordinal_domain()
            width = self.rating_step or 1000
            buckets: dict[int, int] = {}
            for v in ordinals:
                idx = max(0, (v - lo) // width)
                buckets[idx] = buckets.get(idx, 0) + 1
            out = []
            max_idx = max(buckets.keys(), default=0)
            for i in range(max_idx + 1):
                start = lo + i * width
                out.append((f"{start}-{start + width - 1}", buckets.get(i, 0)))
            return out
        # tier-based: считаем по крупным тирам, схлопывая дивизионы
        order: list[str] = []
        counts: dict[str, int] = {}
        for choice in self.rating_choices:
            if choice.major_label not in counts:
                counts[choice.major_label] = 0
                order.append(choice.major_label)
        by_ordinal = {c.ordinal: c for c in self.rating_choices}
        for v in ordinals:
            choice = by_ordinal.get(v)
            if choice:
                counts[choice.major_label] += 1
        return [(label, counts[label]) for label in order]
