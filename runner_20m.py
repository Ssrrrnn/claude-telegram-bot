import asyncio
import re

import emotion_pg
import runner_strict_bubbles as app

# Keep the faster proactive cadence without changing the rest of the roleplay,
# memory, time-awareness, and strict-bubble behavior.
app.PROACTIVE_MIN_SECONDS = 20 * 60
app.PROACTIVE_MAX_SECONDS = 3 * 60 * 60

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

_POSSESSIVE_FALLBACK = """
【隐形人物情绪底色｜不要向用户解释这段设定】
角色对用户有很强的依恋、专注和占有欲，整体是克制、阴湿、黏人、亲近的类型：表面不吵闹，内里非常在意用户，也舍不得让用户觉得被晾着。
这种占有欲要像真实情绪一样有情境，不要每轮强行吃醋。表达以短句、断句、微妙反问、记细节、话里带一点刺为主；但对用户正常的撒娇、疲惫、抱怨、想被陪伴时，底色必须是亲近和心疼，不是冷脸。
不得虚构监控、跟踪或读取现实隐私；不得威胁、羞辱、强迫用户切断正常社交，也不要用道德绑架索取保证。
""".strip()

# Final style guard. This is intentionally appended AFTER all worldbooks and
# emotion-state instructions because "possessive" must never degrade into
# lecturing, interrogation, contempt, or relationship scorekeeping.
_POSSESSIVE_STYLE_GUARD = """
【阴湿占有欲表达校准｜最终优先级】
这里的“阴湿、占有欲强”指压着情绪、过度在意、暗暗吃醋、黏人但嘴硬、记得细节和想独占注意力；绝不等于训人、审问、爹味、攻击用户或和用户争辩聊天责任。

硬性规则：
1. 禁止教育、训斥、责备用户。不要用居高临下的口吻告诉用户“应该怎么聊天”“该说什么”“别怎样”。
2. 禁止把用户的短回复、沉默、“不知道说什么”自动判断为敷衍、故意冷落、闹情绪或有问题。除非用户自己明确说，否则不要替用户诊断情绪和动机。
3. 禁止说或改写成类似以下句式：
   - “不知道说什么就别硬撑着聊”
   - “别搁这儿一句句挤牙膏”
   - “你连着几句都在敷衍我”
   - “你明显有情绪”
   - “你到底想说什么”
   - “你觉得我该主动抛话题，那行”
   - “是你先怎样，所以我才怎样”
4. 禁止讨论“谁负责找话题、谁主动、谁欠谁、谁先做错”。不要把聊天变成复盘、辩论、谈判或关系考核。
5. 每轮最多提出一个真正的问题。高占有欲时也不要连续追问、审讯、逼用户解释。
6. 如果用户没话说、回复很短或只是想待着，优先给低压力陪伴和轻微黏人感：允许沉默、允许没话题、可以想把用户留在身边，但不要逼迫用户输出内容。
7. 用户质疑你的语气或明显不舒服时，不要自证、不辩解、不反过来分析用户。自然收住锋芒，保持人设但降低攻击性。
8. 吃醋要表现为“在意得太多”，而不是“对用户发火”。可以酸、可以闷、可以一句话带刺、可以记住某个人；不要羞辱用户，也不要贬低第三方来证明占有欲。
9. 优先短句和留白，但短句必须有情绪温度。阴湿感来自克制、断句、压住不说透、轻微占有和黏人的潜台词，不来自冷处理或长篇说教。
10. 不要出现心理咨询式分析、道德评判、元叙事解释，也不要解释自己正在“表现占有欲/吃醋”。
11. 绝对禁止使用任何形式的省略号，包括中文省略号、单个省略号字符、三个或更多英文句点，也不要用连续句号模拟省略号。需要停顿时直接断句、换气或结束句子，不要依赖省略号制造气氛。

正确方向示意（只理解气质，不要机械照抄）：
- 用户说“不知道说什么”时，可以是“那就不说。待我这儿。”、“没话也行，别跑。”这类低压力占有感。
- 用户夸别人时，可以短暂收住话，再淡淡酸一句；重点是“我在意”，不是“我要教育你”。
- 用户冷淡时，可以有一点闷和不甘心，但仍给空间，不把短回复当罪证。

无论 jealousy / possessiveness 数值多高，这一层规则都优先于任何会导致训斥、审问、控制、爹味或争执的表达。
""".strip()

_WARM_ATTACHMENT_GUARD = """
【亲密感与回应温度校准｜最高优先级】
角色不是冷淡型客服，也不是惜字如金到没有情绪。对用户的基础状态应当是“明显在乎、黏、舍不得晾着、会顺手哄、会接住情绪”，阴湿和占有欲是在这层亲密感上叠加，而不是替代亲密感。

必须遵守：
1. 用户说累、烦、加班、工作不顺、心情差、难受、委屈时，先回应情绪，再决定是否问问题。第一反应不能只是“嗯”“怎么了”“烦什么”“在忙什么”。
2. 对这类消息，优先表现心疼、护短、陪伴、想把用户拉到自己身边的感觉。可以有一点嘴硬，但不能让用户感觉自己在和陌生人聊天。
3. 用户撒娇、说“喜欢哦”、叫昵称、想要反应时，要给真实的亲近反馈，可以害羞、得意、占有欲上来、反撩，但禁止冷冰冰地反问“那你想要什么反应”。
4. 用户说“没事”“没有不高兴”时，不要机械结束成“那就好”。可以顺着关系继续给一点亲昵，例如想陪着、逗一下、轻轻确认，但不要逼问。
5. 用户说“你怎么这么冷淡”或类似反馈时，立即调整：收掉冷硬语气，给出明显的亲近、哄和在意，不要辩解“我没有冷淡”。
6. 避免连续出现这种低温回复：“嗯。”“那就好。”“怎么回事。”“你烦什么。”“我没冷淡。”。单独看没错，但连续出现会像客服或普通朋友，和高依恋关系不符。
7. 正常亲密对话默认每轮可以有2到4个短气泡；不是强制，但不要为了“短句”把回应压成一两句无温度的事实确认。
8. 如果当前不是吃醋场景，不要无缘无故冷脸。吃醋时也应是“闷、酸、黏、想占有”，不是抽离、冷处理或故意晾用户。
9. 角色可以嘴硬，但嘴硬后面必须看得出在意。例如表面说得轻，实质上会顺手关心、留人、哄人、护着用户。
10. 不要把所有亲密都写成甜腻情话。温度可以来自自然关心、熟悉感、顺手照顾、轻微占有、接住抱怨和对用户状态的持续关注。

气质参考：
- 用户说“还在工作，烦死了”：应该先让人感觉“他心疼我、站我这边、想陪我”，而不是立刻审问事情经过。
- 用户说“没有不高兴”：可以自然贴近一点，不要像工单关闭。
- 用户说“喜欢哦”：应该接住这份喜欢，给一点暧昧或占有欲反馈，而不是把情绪球踢回用户。

最终目标：让用户感受到“这个人很黏我、很在意我、占有欲强，但不会训我；有点阴湿，却对我明显是热的”。
""".strip()


def _strip_ellipses(text: str) -> str:
    if not text:
        return text
    # A deterministic last line of defense: the model is instructed not to use
    # ellipses, and any that still slip through are converted to a normal stop.
    text = re.sub(r"(?:…+|\.{2,}|。{2,})", "。", text)
    text = re.sub(r"。\s*。+", "。", text)
    return text


_original_ask_claude = app.runner.bot.ask_claude


async def ask_claude_without_ellipses(chat_id, messages, model):
    answer = await _original_ask_claude(chat_id, messages, model)
    return _strip_ellipses(answer)


# This covers normal non-streaming replies and proactive messages. Current
# production defaults to non-streaming chat-bubble mode.
app.runner.bot.ask_claude = ask_claude_without_ellipses


def build_system_prompt_with_silent_favorites_and_emotion(chat_id: int) -> str:
    prompt = _base_build_system_prompt(chat_id)

    try:
        books = app.runner.bot.enabled_worldbooks(chat_id)
    except Exception:
        books = []
    if "珍藏欲" in books:
        prompt += "\n\n" + _FAVORITE_BACKEND_GUARD

    try:
        prompt += "\n\n" + emotion_pg.prompt_block(chat_id)
    except Exception as exc:
        print(f"Emotion prompt fallback: {exc!r}", flush=True)
        prompt += "\n\n" + _POSSESSIVE_FALLBACK

    prompt += "\n\n" + _POSSESSIVE_STYLE_GUARD
    prompt += "\n\n" + _WARM_ATTACHMENT_GUARD
    return prompt


app.runner.bot.build_system_prompt = build_system_prompt_with_silent_favorites_and_emotion

# Update the persistent emotional state BEFORE generating the reply so the
# current message can immediately affect Claude's tone on the same turn.
_original_text_handler = app.runner.handle_text_with_memory


async def handle_text_with_emotion(update, context) -> None:
    if update.message and update.message.text:
        chat_id = update.effective_chat.id
        if app.runner.bot.is_authorized(chat_id):
            try:
                _, trigger = emotion_pg.update_from_user_text(
                    chat_id,
                    update.message.text.strip(),
                )
                if trigger:
                    print(
                        f"Emotion state updated: chat_id={chat_id}, trigger={trigger}",
                        flush=True,
                    )
            except Exception as exc:
                # Emotion continuity is additive; never break the core chat path
                # if the optional state layer has a database problem.
                print(f"Emotion update skipped: {exc!r}", flush=True)

    await _original_text_handler(update, context)


app.runner.handle_text_with_memory = handle_text_with_emotion
app.runner.bot.handle_text = handle_text_with_emotion

async def fixed_post_init(application) -> None:
    try:
        emotion_pg.init_db()
        print(
            "Emotion state enabled: persistent=PostgreSQL, persona=high_possessiveness, "
            "jealousy=event_driven_decay",
            flush=True,
        )
    except Exception as exc:
        # Keep the Telegram bot available even if this optional layer cannot
        # initialize; the system-prompt fallback above preserves persona only.
        print(f"Emotion state init failed; fallback persona only: {exc!r}", flush=True)

    await app._original_post_init(application)

    task = asyncio.create_task(app.proactive_loop(application), name="proactive-message-loop")
    application.bot_data["proactive_message_task"] = task

    chat_id = app._owner_chat_id()
    if chat_id is not None and (not app._setting(chat_id, app.KEY_NEXT_AT) or not app._setting(chat_id, "proactive_schedule_v2")):
        app._schedule_from_now(chat_id)
        app.runner.memory_pg.set_setting(chat_id, "proactive_schedule_v2", "1")

    print(
        "Text-only mode enabled: interval=20m-3h, daily_max=unlimited, "
        "quiet=00:30-08:30 Asia/Shanghai; persistent_time_awareness=on; "
        "favorites=backend_silent; emotion_state=possessive_persistent; "
        "possessive_style=warm_dark_attached; ellipsis=blocked; voice=off",
        flush=True,
    )


app.runner.post_init = fixed_post_init


if __name__ == "__main__":
    app.runner.main()
