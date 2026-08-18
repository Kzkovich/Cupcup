from fastapi import Request


def flash(request: Request, text: str, level: str = "info") -> None:
    bucket = request.session.setdefault("flash", [])
    bucket.append({"level": level, "text": text})


def pop_flashed(request: Request) -> list[dict]:
    return request.session.pop("flash", [])
