from fastapi import Request


def flash(request: Request, text: str, level: str = "info") -> None:
    # Присваиваем новый список целиком: SessionMiddleware обновляет cookie только при
    # присваивании ключа, а append() в уже существующий список изменения не замечает.
    request.session["flash"] = [*request.session.get("flash", []), {"level": level, "text": text}]


def pop_flashed(request: Request) -> list[dict]:
    return request.session.pop("flash", [])
