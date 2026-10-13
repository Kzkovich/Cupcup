"""Запуск сквозного сценария в ОТДЕЛЬНОМ процессе на ВРЕМЕННОЙ базе.

Реальная база не затрагивается вообще: сценарий создаёт десятки аккаунтов и команд,
и посетители сайта не должны это видеть. Поэтому БД — пустой файл во временной папке.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
MARK = "@@RESULT@@"


def run_isolated(timeout: int = 240) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="scenario-", dir=_tmp_parent()))
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp / 'scenario.db'}",
        "SCENARIO_ISOLATED": "1",
        "ADMIN_EMAILS": "qa.admin@mail.invalid",
        "INVITE_CODE": "",
        "TG_BOT_TOKEN": "",
        "TG_BOT_USERNAME": "",
        "SESSION_COOKIE_SECURE": "true",
        "ENV": "scenario",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.demo.scenario"],
            cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=timeout,
        )
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith(MARK):
                return json.loads(line[len(MARK):])
        return _crash(f"Сценарий не вернул результат (код {proc.returncode})", proc.stderr[-800:])
    except subprocess.TimeoutExpired:
        return _crash(f"Сценарий не уложился в {timeout} с", "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tmp_parent() -> str:
    data = APP_ROOT / "data"
    return str(data) if data.is_dir() and os.access(data, os.W_OK) else tempfile.gettempdir()


def _crash(msg: str, detail: str) -> dict:
    return {"steps": [{"section": "—", "name": msg, "ok": False, "detail": detail}],
            "total": 1, "passed": 0, "failed": 1, "seconds": 0, "error": msg}
