"""Pure scheduling and repetition rules; no model, Telegram or database calls."""
import random
import re
from datetime import timedelta
from difflib import SequenceMatcher

MIN_SECONDS = 20 * 60
MAX_SECONDS = 3 * 60 * 60
TYPES = ("想念", "关心", "延续话题", "分享", "吃醋", "深夜陪伴", "调侃")


def is_quiet(dt):
    return 30 <= dt.hour * 60 + dt.minute < 8 * 60 + 30


def after_quiet(dt):
    target = dt.replace(hour=8, minute=30, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return target + timedelta(minutes=random.randint(0, 25))


def next_due(dt, silence_count=0):
    # Unanswered messages become less frequent, but never disable contact.
    lower = (20, 40, 60, 90)[min(max(silence_count, 0), 3)] * 60
    due = dt + timedelta(seconds=random.randint(lower, MAX_SECONDS))
    return after_quiet(due) if is_quiet(due) else due


def choose_type(dt, recent_types, silence_count):
    choices = [kind for kind in TYPES if kind not in recent_types[-2:]]
    if not (dt.hour >= 23 or dt.hour < 6):
        choices = [kind for kind in choices if kind != "深夜陪伴"]
    # Silence is not evidence of a rival or a reason to interrogate the user.
    if silence_count:
        choices = [kind for kind in choices if kind not in ("吃醋", "关心")]
    return random.choice(choices or ["分享", "想念", "调侃"])


def normalized(text):
    return re.sub(r"[\W_]+", "", text.casefold())


def is_repetition(text, previous):
    value = normalized(text)
    if not value:
        return True
    for old in previous:
        other = normalized(old)
        if not other:
            continue
        if value == other or SequenceMatcher(None, value, other).ratio() >= 0.72:
            return True
        if min(len(value), len(other)) >= 6 and (value in other or other in value):
            return True
    return False
