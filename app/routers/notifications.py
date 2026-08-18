from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_user
from app.models import Notification, User
from app.notify import describe
from app.templating import render

router = APIRouter(tags=["notifications"])


@router.get("/notifications")
def notifications_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    items = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    rows = [{"n": n, "meta": describe(n), "was_unread": n.read_at is None} for n in items]

    unread_ids = [n.id for n in items if n.read_at is None]
    if unread_ids:
        db.query(Notification).filter(Notification.id.in_(unread_ids)).update(
            {"read_at": datetime.utcnow()}, synchronize_session=False
        )
        db.commit()

    return render(request, "notifications.html", user=user, game=None, rows=rows, notif_unread_count=0)
