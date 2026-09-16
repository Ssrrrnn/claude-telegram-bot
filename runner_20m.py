import asyncio

import runner_strict_bubbles as app

# Keep the faster proactive cadence without changing the rest of the roleplay,
# memory, time-awareness, and strict-bubble behavior.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 60 * 60

# Quiet hours are 00:30-08:30 Asia/Shanghai.  The previous implementation
# compared minutes-since-midnight against 30, which accidentally made nearly
# the entire day quiet.
QUIET_START_MINUTE_OF_DAY = 30
QUIET_END_MINUTE_OF_DAY = 8 * 60 + 30


def fixed_is_quiet_time(dt=None) -> bool:
    dt = dt or app._now_local()
    minute_of_day = dt.hour * 60 + dt.minute
    return QUIET_START_MINUTE_OF_DAY <= minute_of_day < QUIET_END_MINUTE_OF_DAY


def fixed_next_after_quiet(now_local=None) -> float:
    now_local = now_local or app._now_local()
    target = now_local.replace(
        hour=app.QUIET_END_HOUR,
        minute=app.QUIET_END_MINUTE,
        second=0,
        microsecond=0,
    )
    if now_local >= target:
        target += app.timedelta(days=1)
    target += app.timedelta(minutes=app.random.randint(0, 25))
    return target.timestamp()


def fixed_schedule_from_now(chat_id: int) -> None:
    now = app.time.time()
    next_at = now + app._random_delay()
    local_due = app.datetime.fromtimestamp(next_at, app.PROACTIVE_TZ)
    if fixed_is_quiet_time(local_due):
        next_at = fixed_next_after_quiet(local_due)

    app._set_setting(chat_id, app.KEY_NEXT_AT, str(int(next_at)))
    scheduled = app.datetime.fromtimestamp(next_at, app.PROACTIVE_TZ)
    print(
        f"Next proactive message scheduled: chat_id={chat_id}, at={scheduled:%Y-%m-%d %H:%M:%S %Z}",
        flush=True,
    )


# Patch every caller in runner_strict_bubbles to use the corrected scheduling.
app._is_quiet_time = fixed_is_quiet_time
app._next_after_quiet = fixed_next_after_quiet
app._schedule_from_now = fixed_schedule_from_now


# Start the background loop directly on the running asyncio loop.  This avoids
# python-telegram-bot warning that Application.create_task() was called before
# Application.running became true during post_init.
async def fixed_post_init(application) -> None:
    await app._original_post_init(application)

    task = asyncio.create_task(app.proactive_loop(application), name="proactive-message-loop")
    application.bot_data["proactive_message_task"] = task

    chat_id = app._owner_chat_id()
    if chat_id is not None and not app._setting(chat_id, app.KEY_NEXT_AT):
        fixed_schedule_from_now(chat_id)

    print(
        "Proactive messaging enabled: interval=20m-60m, daily_max=8, "
        "quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on",
        flush=True,
    )


app.runner.post_init = fixed_post_init


if __name__ == "__main__":
    app.runner.main()
