import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from app import Store, create_app, publication_watch, PUBLICATION_INTERVAL
from publication_vk import normalize, scan_wall, date_timestamp
from vk_api import VKError, UncertainDelivery


class PublicationStoreTests(unittest.TestCase):
    def setUp(self):
        legacy = patch.dict('os.environ', {'VK_POSTER_LEGACY_BROWSER':'1'})
        legacy.start()
        self.addCleanup(legacy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.group = {'id': 'group', 'name': 'Test', 'url': 'https://vk.ru/club123',
                      'mode': 'browser_suggest', 'browser_checked': 'browser_suggest',
                      'enabled': True, 'interval_hours': 24, 'next_at': 0}
        self.store.state['groups'] = [self.group]
        self.store.state['draft'] = {'text': 'Original', 'photos': ['a.jpg']}

    def tearDown(self):
        self.store.browser.executor.shutdown()
        self.temp.cleanup()

    def submitted(self, unknown=False):
        task = self.store.claim('group')
        def post(action, group, draft, folder, callback):
            callback(['-123_1'])
            if unknown:
                raise UncertainDelivery('Unknown')
            return {'status': 'browser_suggested', 'url': '', 'detail': 'Accepted'}
        with patch.object(self.store.browser, 'call', side_effect=post):
            self.store.deliver(task)
        return self.store.state['history'][0]

    def test_snapshot_persisted_before_io_and_survives_restart(self):
        task = self.store.claim('group')
        restored = Store(self.temp.name)
        restored.recover()
        h = restored.state['history'][0]
        self.assertEqual(h['status'], 'unknown')
        self.assertEqual(h['publication']['state'], 'pending')
        self.assertEqual(h['publication']['text'], 'Original')
        self.assertFalse(restored.group('group')['enabled'])
        self.store.state['draft']['text'] = 'Edited'
        self.assertEqual(task[1]['text'], 'Original')
        restored.browser.executor.shutdown()

    def test_pending_blocks_both_manual_and_scheduled_resend(self):
        h = self.submitted()
        self.assertEqual(h['publication']['known_posts'], ['-123_1'])
        with self.assertRaisesRegex(ValueError, 'ещё проверяется'):
            self.store.claim('group')
        self.group['next_at'] = 0
        with patch.object(self.store.browser, 'call') as browser:
            self.store.tick()
        browser.assert_not_called()

    def test_due_monitoring_even_while_schedule_paused_and_found_preserves_pause(self):
        h = self.submitted(unknown=True)
        self.assertFalse(self.group['enabled'])
        self.group.pop('delivery_pause', None)  # Explicit user pause overrides restoration.
        h['publication']['next_check'] = 0
        with patch.object(self.store.browser, 'call', return_value={'state':'found', 'url':'https://vk.ru/wall-123_2', 'detail':'Match'}) as browser:
            self.store.tick()
        self.assertEqual(browser.call_args.args[0], 'publication')
        self.assertEqual(h['publication']['state'], 'found')
        self.assertFalse(self.group['enabled'])
        self.assertEqual(h['status'], 'published')
        self.assertEqual(h['delivery_status'], 'unknown')
        self.assertEqual(h['url'], 'https://vk.ru/wall-123_2')
        self.assertFalse(self.store.busy)

    def test_receipt_restores_automatic_pause_but_blocks_duplicate_until_publication(self):
        h = self.submitted(unknown=True)
        with patch.object(self.store.browser, 'call', return_value={
                'state':'pending', 'suggested':True, 'suggestion_url':'https://vk.ru/wall-123?suggested=1',
                'url':'', 'detail':'Awaiting moderation'}):
            self.store.check_publication(self.store.claim_publication(h['id']))
        self.assertEqual(h['status'], 'browser_suggested')
        self.assertEqual(h['delivery_status'], 'unknown')
        self.assertTrue(self.group['enabled'])
        with self.assertRaisesRegex(ValueError, 'ещё проверяется'):
            self.store.claim('group')

    def test_failed_check_not_rejection_and_retries_later(self):
        h = self.submitted()
        with patch.object(self.store.browser, 'call', side_effect=VKError('No access')):
            self.store.check_publication(self.store.claim_publication(h['id']))
        self.assertEqual(h['publication']['state'], 'error')
        self.assertGreater(h['publication']['next_check'], time.time()+PUBLICATION_INTERVAL-5)
        self.assertEqual(h['status'], 'browser_suggested')
        self.assertFalse(self.store.busy)

    def test_pending_history_retained_past_200_entries(self):
        h = self.submitted()
        for i in range(205):
            self.store.add_history({'id':str(i),'status':'error'})
        self.assertIn(h, self.store.state['history'])

    def test_check_claim_is_exclusive(self):
        h = self.submitted()
        self.store.claim_publication(h['id'])
        with self.assertRaisesRegex(ValueError, 'занят'):
            self.store.claim_publication(h['id'])

    def test_old_history_requires_explicit_current_draft_choice_and_stop_pauses(self):
        import re
        # A separate fresh app loads the saved state.
        self.store.state['history']=[{'id':'old','group_id':'group','status':'unknown','time':time.time()}]
        self.store.save()
        app=create_app(self.temp.name);client=app.test_client()
        headers={'X-CSRF-Token':re.search('name="csrf-token" content="([^"]+)"',client.get('/').text)[1]}
        path='/api/history/old/publication/'
        self.assertEqual(client.post(path+'start',json={},headers=headers).status_code,400)
        self.assertEqual(client.post(path+'start',json={'use_current_draft':True},headers=headers).status_code,200)
        watch=app.store.state['history'][0]['publication']
        self.assertEqual(watch['source'],'current_draft')
        self.assertEqual(client.post(path+'stop',json={},headers=headers).status_code,200)
        self.assertEqual(watch['state'],'stopped')
        self.assertFalse(app.store.group('group')['enabled'])
        app.store.browser.executor.shutdown()


class WallReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path('.browsers').resolve()))
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop()

    def setUp(self):
        self.page=self.browser.new_page()
        self.watch={'group_url':'https://vk.ru/club123','text':'Hello https://vk.com/team','photo_count':1,'since':time.time()-120}

    def tearDown(self):
        self.page.close()

    def post(self, pid=2, ts=None, text=None, owner=123):
        return f'''<div data-testid="post" data-post-id="-{owner}_{pid}" data-post-nesting-lvl="0">
        <span data-testid="post_text">{text or 'Hello https://vk.com/team'}<span data-testid="post-footer-author">Signature</span></span>
        <a href="/photo-123_55"><img></a><a href="/photo-123_55">Same photo</a>
        <a data-testid="post_date_block_preview" data-ts="{int(ts if ts is not None else time.time())}" href="/wall-{owner}_{pid}">Today</a></div>'''

    def scan(self, html):
        self.page.set_content(html)
        return scan_wall(self.page,self.watch,max_pages=1)

    def test_found_full_text_date_and_unique_photos(self):
        result=self.scan(self.post())
        self.assertEqual(result['state'],'found');self.assertEqual(result['url'],'https://vk.ru/wall-123_2')

    def test_suggested_queue_date_is_recognized_only_in_suggested_mode(self):
        html=self.post()
        import re
        html=re.sub(r'<a data-testid="post_date_block_preview".*?</a>',
                    '<span data-testid="post-header-subtitle-date">1 мин назад</span>',html)
        self.page.set_content(html)
        result=scan_wall(self.page,self.watch,max_pages=1,suggested=True)
        self.assertEqual(result['state'],'found')
        self.watch['since']=time.time()+600
        self.assertEqual(scan_wall(self.page,self.watch,max_pages=1,suggested=True)['state'],'pending')

    def test_old_same_text_wrong_owner_and_known_posts_ignored(self):
        self.watch['known_posts']=['-123_3']
        result=self.scan(self.post(ts=time.time()-86400)+self.post(pid=3)+self.post(pid=5,owner=999))
        self.assertEqual(result['state'],'pending')

    def test_edited_text_missing_photos_and_duplicate_matches_not_confirmed(self):
        self.assertEqual(self.scan(self.post(text='Edited'))['state'],'pending')
        self.watch['photo_count']=2
        self.assertEqual(self.scan(self.post())['state'],'pending')
        self.watch['photo_count']=1
        self.assertEqual(self.scan(self.post()+self.post(pid=3))['state'],'pending')

    def test_nested_repost_not_confirmed(self):
        html=self.post().replace('<span data-testid="post_text">','<div class="copy_quote"></div><span data-testid="post_text">')
        self.assertEqual(self.scan(html)['state'],'pending')

    def test_legacy_markup(self):
        self.assertEqual(self.scan(f'<div id="post-123_8"><div class="wall_post_text">Hello https://vk.com/team</div><a href="/photo-123_9">Photo</a><a class="post_link"><span data-ts="{int(time.time())}">Today</span></a></div>')['state'],'found')

    def test_tooltip_year_boundary_and_text_normalization(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,1,1,12,tzinfo=ZoneInfo('Europe/Moscow')).timestamp()
        parsed=date_timestamp('31 дек в 15:514 просмотра',now,'Europe/Moscow')
        self.assertEqual(datetime.fromtimestamp(parsed,ZoneInfo('Europe/Moscow')).year,2025)
        self.assertIsNone(date_timestamp('yesterday',now,'Europe/Moscow'))
        self.assertEqual(normalize('A\u00a0https://vk.ru/team'),'A vk.com/team')

if __name__=='__main__':unittest.main()
