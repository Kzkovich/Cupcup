"""Миграции Alembic должны применяться с нуля и совпадать с моделями —
иначе после деплоя на проде не хватит колонки."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _alembic(*args, db):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_migrations_apply_from_scratch_and_match_models(tmp_path):
    db = tmp_path / "m.db"
    up = _alembic("upgrade", "head", db=db)
    assert up.returncode == 0, up.stderr[-800:]
    check = _alembic("check", db=db)
    assert check.returncode == 0, "Модели изменились без миграции:\n" + (check.stdout + check.stderr)[-800:]
