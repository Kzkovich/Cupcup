from app.games.base import GameConfig
from app.games.dota2 import DOTA2
from app.games.lol import LOL

GAMES: dict[str, GameConfig] = {
    DOTA2.key: DOTA2,
    LOL.key: LOL,
}


def get_game(key: str) -> GameConfig | None:
    return GAMES.get(key)


def game_or_404(key: str) -> GameConfig:
    game = GAMES.get(key)
    if game is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Unknown game")
    return game
