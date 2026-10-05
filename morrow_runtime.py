"""Run the existing companion without Telegram polling or a QQ connection."""
import asyncio
import os
os.environ["MORROW_ONLY"] = "1"
from types import SimpleNamespace
import runner_20m

async def main():
    runner = runner_20m.app.runner
    runner.bot.require_config()
    await asyncio.to_thread(runner.memory_pg.init_db)
    class LocalTransport:
        async def set_my_commands(self, commands): pass
        async def send_message(self, **kwargs):
            raise RuntimeError('External messaging is disabled in Morrow mode')
    application = SimpleNamespace(bot_data={}, bot=LocalTransport())
    await runner_20m.fixed_post_init(application)
    if 'ios_api_runner' not in application.bot_data:
        raise RuntimeError('Morrow API did not start')
    owner = runner.bot.TELEGRAM_OWNER_ID
    if runner.memory_pg.get_setting(owner, 'morrow_transport_migrated') != '1':
        runner.memory_pg.set_setting(owner, 'proactive_enabled', '1')
        runner.memory_pg.set_setting(owner, 'companion_auto_diary_v1', '1')
        runner.memory_pg.set_setting(owner, 'last_active_platform_v1', 'ios')
        heartbeat = application.bot_data.get('companion_heartbeat')
        if heartbeat is not None: await asyncio.to_thread(heartbeat.store.configure, True)
        runner.memory_pg.set_setting(owner, 'morrow_transport_migrated', '1')
    print('Morrow ready: telegram=disabled qq=disabled proactive=ios', flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for value in application.bot_data.values():
            if isinstance(value, asyncio.Task): value.cancel()
        await runner.shutdown(application)

if __name__ == '__main__':
    asyncio.run(main())
