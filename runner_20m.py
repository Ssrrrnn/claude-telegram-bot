import asyncio

import runner_strict_bubbles as app

# Keep the faster proactive cadence without changing the rest of the roleplay,
# memory, time-awareness, and strict-bubble behavior.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 60 * 60

# The actual favorite feature is implemented by the PostgreSQL memory analyzer.
# The main chat model must never invent or expose a fake tool-call protocol.
_base_build_system_prompt = app.runner.bot.build_system_prompt
_FAVORITE_BACKEND_GUARD = """
【收藏功能实现说明｜最高优先级】
“收藏/珍藏”功能由聊天程序后台独立判断并写入数据库，主对话模型不拥有、也不需要调用任何收藏工具。
你只负责像角色本人一样正常回复用户。
绝对禁止在正文中输出、模仿、拼接或解释任何收藏工具调用、XML、JSON、函数参数或内部协议。
尤其禁止输出 <favorite_message>、</favorite_message>、<messageid>、</messageid>、<mood>、</mood>，以及任何相似标签。
不要为了表示收藏而发送“已收藏”“收藏成功”、消息ID、心情字段或后台状态。
如果某句话让角色觉得值得珍惜，只需继续符合人设地自然聊天；真实收藏由后台静默完成。
""".strip()


def build_system_prompt_with_silent_favorites(chat_id: int) -> str:
    prompt = _base_build_system_prompt(chat_id)
    try:
        books = app.runner.bot.enabled_worldbooks(chat_id)
    except Exception:
        books = []
    if "珍藏欲" in books:
        prompt += "\n\n" + _FAVORITE_BACKEND_GUARD
    return prompt


app.runner.bot.build_system_prompt = build_system_prompt_with_silent_favorites

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


app._is_quiet_time = fixed_is_quiet_time
app._next_after_quiet = fixed_next_after_quiet
app._schedule_from_now = fixed_schedule_from_now


async def fixed_post_init(application) -> None:
    await app._original_post_init(application)

    task = asyncio.create_task(app.proactive_loop(application), name="proactive-message-loop")
    application.bot_data["proactive_message_task"] = task

    chat_id = app._owner_chat_id()
    if chat_id is not None and not app._setting(chat_id, app.KEY_NEXT_AT):
        fixed_schedule_from_now(chat_id)

    print(
        "Text-only mode enabled: interval=20m-60m, daily_max=8, "
        "quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on; "
        "favorites=backend_silent; voice=off",
        flush=True,
    )


app.runner.post_init = fixed_post_init


if __name__ == "__main__":
    app.runner.main()
