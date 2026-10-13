"""CLI: python -m app.demo seed | purge | status | scenario"""
import json
import sys

from app.db import SessionLocal
from app.demo.purge import demo_counts, purge_demo
from app.demo.seed import AlreadySeeded, seed_demo


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "scenario":
        from app.demo.scenario_runner import run_isolated
        report = run_isolated()
        for s in report["steps"]:
            print(("PASS " if s["ok"] else "FAIL ") + s["name"] + (f" — {s['detail']}" if s["detail"] and not s["ok"] else ""))
        print(f"\n{report['passed']}/{report['total']} шагов прошли за {report['seconds']}s")
        return 0 if report["failed"] == 0 else 1

    with SessionLocal() as db:
        if cmd == "seed":
            try:
                print(json.dumps(seed_demo(db), ensure_ascii=False))
            except AlreadySeeded as e:
                print(f"Пропущено: {e}")
                return 1
        elif cmd == "purge":
            print(json.dumps(purge_demo(db), ensure_ascii=False))
        elif cmd == "status":
            print(json.dumps(demo_counts(db), ensure_ascii=False))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
