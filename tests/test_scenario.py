"""Сквозной сценарий: весь клиентский путь + админка + демо-данные на временной БД."""
from app.demo.scenario_runner import run_isolated


def test_full_scenario():
    report = run_isolated(timeout=300)
    failed = [f"[{s['section']}] {s['name']} — {s['detail']}" for s in report["steps"] if not s["ok"]]
    assert report["total"] > 100, f"сценарий выполнился подозрительно коротко: {report['total']} шагов"
    assert not failed, "Упали шаги сценария:\n" + "\n".join(failed)
