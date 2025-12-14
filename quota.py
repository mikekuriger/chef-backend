# quota.py
from __future__ import annotations  # postpone annotation evaluation
from typing import TYPE_CHECKING, Any

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, update, and_, text as sqltext
from sqlalchemy.exc import IntegrityError, OperationalError

PT = ZoneInfo("America/Los_Angeles")
WEEKLY_TEXT_QUOTA = 3                   # temp for testing
FREE_IMAGE_QUOTA  = 0                   # temp for testing

if TYPE_CHECKING:
    # For editors only; never runs at runtime
    from app import UserCredits

def _pt_week_anchor(dt_utc: datetime) -> datetime:
    now_pt = dt_utc.astimezone(PT)
    # floor to Sunday 00:00 PT
    days_to_sunday = (now_pt.weekday() + 1) % 7  # Mon=0..Sun=6 => days back to Sunday
    sunday = (now_pt - timedelta(days=days_to_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    return sunday.astimezone(timezone.utc).replace(tzinfo=None)  # naive UTC for DB

def _next_reset_iso(anchor_utc: datetime) -> str:
    return (anchor_utc + timedelta(days=7)).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

def _models():
    # late import avoids circular import at module import time
    from app import db, UserCredits
    return db, UserCredits

def get_or_create_credits(user_id: int, max_retries: int = 3) -> "UserCredits":
    db, UserCredits = _models()

    for attempt in range(max_retries):
        try:
            # Fast path: already exists
            uc = UserCredits.query.get(user_id)
            if uc:
                return uc

            # Create new credits row
            anchor = _pt_week_anchor(datetime.utcnow())
            uc = UserCredits(
                user_id=user_id,
                text_remaining_week=WEEKLY_TEXT_QUOTA,
                image_remaining_lifetime=FREE_IMAGE_QUOTA,
                # image_remaining_lifetime=3,
                week_anchor_utc=anchor,
            )
            db.session.add(uc)
            db.session.commit()
            return uc

        except IntegrityError:
            # Most likely: another transaction inserted the row first
            db.session.rollback()
            uc = UserCredits.query.get(user_id)
            if uc:
                return uc
            # If still nothing, something else is wrong
            raise

        except OperationalError as e:
            db.session.rollback()
            # MariaDB/MySQL deadlock = 1213
            orig = getattr(e, "orig", None)
            code = orig.args[0] if (orig and getattr(orig, "args", None)) else None
            if code == 1213 and attempt < max_retries - 1:
                # Retry the loop
                continue
            raise


# def get_or_create_credits(user_id: int) -> "UserCredits":
#     db, UserCredits = _models()
#     uc = UserCredits.query.get(user_id)
#     if uc:
#         return uc
#     anchor = _pt_week_anchor(datetime.utcnow())
#     uc = UserCredits(
#         user_id=user_id,
#         text_remaining_week=WEEKLY_TEXT_QUOTA,
#         image_remaining_lifetime=3,
#         week_anchor_utc=anchor
#     )
#     db.session.add(uc)
#     db.session.commit()
#     return uc

def ensure_week_current(user_id: int) -> "UserCredits":
    db, UserCredits = _models()
    now = datetime.utcnow()
    cur_anchor = _pt_week_anchor(now)
    # lock row
    uc = db.session.execute(
        select(UserCredits).where(UserCredits.user_id == user_id).with_for_update()
    ).scalars().first()
    if not uc:
        uc = get_or_create_credits(user_id)
        # lock it after creation
        uc = db.session.execute(
            select(UserCredits).where(UserCredits.user_id == user_id).with_for_update()
        ).scalars().first()

    if cur_anchor > (uc.week_anchor_utc or cur_anchor):
        uc.week_anchor_utc = cur_anchor
        uc.text_remaining_week = WEEKLY_TEXT_QUOTA
        db.session.commit()
    return uc

def decrement_text_or_deny(user_id: int) -> tuple[bool, str | None]:
    db, UserCredits = _models()
    uc = ensure_week_current(user_id)
    if uc.text_remaining_week <= 0:
        return False, _next_reset_iso(uc.week_anchor_utc)
    uc.text_remaining_week -= 1
    db.session.commit()
    return True, None

def refund_text(user_id: int) -> None:
    db, UserCredits = _models()
    uc = UserCredits.query.get(user_id)
    if uc:
        uc.text_remaining_week += 1
        db.session.commit()

def decrement_image_or_deny(user_id: int) -> bool:
    db, UserCredits = _models()
    uc = get_or_create_credits(user_id)
    if uc.image_remaining_lifetime <= 0:
        return False
    uc.image_remaining_lifetime -= 1
    db.session.commit()
    return True

def refund_image(user_id: int) -> None:
    db, UserCredits = _models()
    uc = UserCredits.query.get(user_id)
    if uc:
        uc.image_remaining_lifetime += 1
        db.session.commit()

def next_reset_iso(user_id: int) -> str:
    db, UserCredits = _models()
    uc = get_or_create_credits(user_id)
    return _next_reset_iso(uc.week_anchor_utc)

