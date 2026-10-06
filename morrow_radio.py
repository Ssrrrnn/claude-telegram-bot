"""Owner-only radio narration using the companion's existing voice settings.

No chat turn, diary, or memory is created. Only explicitly selected text is read.
Provider credentials and voice IDs never leave the service.
"""
import asyncio
import base64
import hashlib
import re
import time
from collections import OrderedDict

import voice_output


class RadioError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class RadioSpeech:
    def __init__(self, memory, owner):
        self.memory, self.owner = memory, owner
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()

    def settings(self):
        keys = [voice_output.BACKEND_KEY, voice_output.PROFILE_KEY,
                voice_output.CUSTOM_VOICE_KEY, voice_output.FISH_VOICE_KEY]
        values = {key: self.memory.get_setting(self.owner, key) for key in keys} if self.memory else {}
        getter = lambda owner, key: values.get(key)
        provider = voice_output.backend(getter, self.owner)
        profile = voice_output.profile(getter, self.owner)
        voice = (voice_output.fish_voice(getter, self.owner) if provider == 'fish' else
                 values.get(voice_output.CUSTOM_VOICE_KEY, '') if provider == 'cosyvoice' else '')
        return provider, profile, voice or ''

    async def read(self, data):
        if not isinstance(data, dict) or set(data) - {'text', 'tone'}:
            raise RadioError(400, '电台内容格式不正确。')
        text, tone = data.get('text'), data.get('tone', 'night')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 450:
            raise RadioError(400, '每段朗读需要 1–450 字。')
        if tone not in ('night', 'natural'):
            raise RadioError(400, '请选择电台的朗读风格。')
        # Do not accept caller-supplied Fish delivery instructions or provider options.
        speech = voice_output.clean_speech(re.sub(r'\[[A-Za-z][A-Za-z -]{0,60}\]', '', text))
        if not speech:
            raise RadioError(400, '这段内容没有可朗读的文字。')
        if self.lock.locked():
            raise RadioError(429, '正在准备上一段声音，请稍后重试。')
        async with self.lock:
            provider, profile, voice = await asyncio.to_thread(self.settings)
            key = hashlib.sha256('\0'.join((provider, profile, voice, tone, speech)).encode()).hexdigest()
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 600:
                self.cache.move_to_end(key)
                return cached[1]
            synthesis = '[soft tone] ' + speech if provider == 'fish' and tone == 'night' else speech
            try:
                audio = await asyncio.wait_for(voice_output.synthesize(synthesis, profile, provider, voice), timeout=55)
                if not isinstance(audio, bytes) or not audio or len(audio) > voice_output.MAX_AUDIO_BYTES:
                    raise ValueError('Invalid narration audio')
            except Exception as exc:
                print(f'Radio narration unavailable: {type(exc).__name__}', flush=True)
                raise RadioError(503, '这段声音暂时没有准备好，文字仍在，可以重试。') from None
            result = {'audio_base64': base64.b64encode(audio).decode(), 'text': speech,
                      'voice': '伴侣音色', 'provider': provider}
            self.cache[key] = (time.monotonic(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
            return result
