import unittest
from unittest.mock import AsyncMock, patch
import morrow_music as music
from phone_bridge import BridgeError


class MusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_requires_real_search_result_and_is_deduplicated(self):
        turn = {}
        with self.assertRaises(BridgeError):
            await music.execute('share_music', {'song_mid': 'invented'}, turn)
        data = {'code': 0, 'data': {'song': {'itemlist': [
            {'mid': 'Test123', 'name': 'Song', 'singer': 'Artist'},
            {'mid': '../private', 'name': 'Invalid'}, {'mid': 'Test123', 'name': 'duplicate'}]}}}
        with patch.object(music, 'fetch_catalog', AsyncMock(return_value=data)):
            result = await music.execute('search_music', {'query': 'Song'}, turn)
        self.assertEqual(len(result['items']), 1)
        for _ in range(2):
            await music.execute('share_music', {'song_mid': 'Test123'}, turn)
        text = music.attach_shares('想把这首给你。', turn)
        self.assertEqual(text.count('https://y.qq.com/n/ryqq/songDetail/Test123'), 1)
        self.assertEqual(music.attach_shares(text, turn), text)
        with self.assertRaises(BridgeError):
            await music.execute('share_music', {'song_mid': 'Unknown'}, turn)

    async def test_failed_search_has_no_shareable_candidates(self):
        turn = {}
        with patch.object(music, 'fetch_catalog', AsyncMock(side_effect=RuntimeError('private details'))):
            with self.assertRaises(BridgeError) as error:
                await music.execute('search_music', {'query': 'Song'}, turn)
        self.assertNotIn('private details', str(error.exception))
        self.assertNotIn('music_candidates', turn)

    def test_unverified_or_failed_metadata_is_not_a_song(self):
        with self.assertRaises(BridgeError):
            music.catalog_items({'code': 1000, 'data': {}})
        self.assertEqual(music.catalog_items({'code': 0, 'data': {'song': {'itemlist': [{'mid': 'A', 'name': ''}]}}}), [])
