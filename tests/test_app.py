import io
import re
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image
import requests

from app import Store, create_app, group_slug
from vk_api import VK, VKError, UncertainDelivery


class AppTests(unittest.TestCase):
    def setUp(self):
        legacy = patch.dict('os.environ', {'VK_POSTER_LEGACY_BROWSER':'1'})
        legacy.start()
        self.addCleanup(legacy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.app.testing = True
        self.client = self.app.test_client()
        page = self.client.get('/').text
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page)[1]
        self.headers = {'X-CSRF-Token': csrf}
        self.store = self.app.store

    def tearDown(self):
        self.temp.cleanup()

    def api(self, method, path, body=None):
        return self.client.open('/api/' + path, method=method, json=body, headers=self.headers)

    def group(self):
        self.api('POST', 'groups', {'url': 'https://vk.ru/club123'})
        self.store.state['groups'][0]['mode']='manual_wall'
        return self.store.state['groups'][0]

    def ready(self):
        g = self.group()
        g.update(mode='api', vk_id=123, enabled=True, next_at=time.time()-1)
        self.store.token = 'test-token-do-not-log'
        self.store.account = {'id': 5, 'name': 'Test'}
        self.store.state['draft']['text'] = 'Test post'
        self.store.save()
        return g

    def test_shared_browser_busy_defers_without_disabling_schedule(self):
        from browser_pool import BrowserBusy
        g = self.ready()
        g.update(mode='browser_suggest', browser_checked='browser_suggest')
        task = self.store.claim(g['id'], scheduled=True)
        with patch.object(self.store.browser, 'call', side_effect=BrowserBusy('Занят входом')):
            self.store.deliver(task)
        self.assertTrue(g['enabled'])
        self.assertEqual(self.store.state['history'][0]['status'], 'deferred')
        self.assertEqual(self.store.state['history'][0]['publication']['state'], 'stopped')
        self.assertFalse(self.store.busy)
        self.assertLess(g['next_at'] - time.time(), 61)

    def test_disabled_operator_cannot_claim_manual_or_scheduled_send(self):
        g = self.ready()
        self.store.operator_active = lambda: False
        for scheduled in (True, False):
            with self.assertRaises(ValueError):
                self.store.claim(g['id'], scheduled=scheduled)
        self.assertEqual(self.store.state['history'], [])

    def test_csrf_host_and_origin(self):
        self.assertEqual(self.client.post('/api/groups', json={'url': 'club1'}).status_code, 403)
        headers = {**self.headers, 'Origin': 'https://evil.example'}
        self.assertEqual(self.client.post('/api/pause', headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/api/state', headers={'Host': 'evil.example'}).status_code, 400)

    def test_group_links_and_duplicates(self):
        for value in ('https://evil.test/club1', 'https://vk.ru/wall-123_4', 'id123', 'https://vk.ru/../../etc'):
            with self.assertRaises(ValueError):
                group_slug(value)
        self.assertEqual(group_slug('123'), 'club123')
        g = self.group()
        self.assertFalse(g['enabled'])
        self.assertEqual(self.api('POST', 'groups', {'url': 'https://vk.com/club123'}).status_code, 400)

    def test_photo_draft_roundtrip_and_conflict(self):
        stream = io.BytesIO()
        Image.new('RGB', (30, 20), 'green').save(stream, format='PNG')
        stream.seek(0)
        response = self.client.post('/api/photos', data={'photo': (stream, 'test.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        name = response.json['name']
        with self.client.get('/photos/' + name) as photo_response:
            self.assertEqual(photo_response.status_code, 200)
        revision = self.store.state['draft_revision']
        self.assertEqual(self.api('PUT', 'draft', {'text':'Changed','photos':[name],'revision':revision}).status_code, 200)
        self.assertEqual(Store(self.temp.name).state['draft']['photos'], [name])
        self.assertEqual(self.api('PUT', 'draft', {'text':'Stale','photos':[],'revision':revision}).status_code, 409)
        self.assertEqual(self.api('PUT', 'draft', {'text':'Bad','photos':['../../x'],'revision':revision+1}).status_code, 400)
        self.assertEqual(self.api('PUT', 'draft', {'text':'New','photos':[],'revision':revision+1}).status_code, 200)

    def test_schedule_changes_do_not_conflict_with_draft(self):
        revision = self.store.state['draft_revision']
        self.group()
        self.assertEqual(self.api('PUT','draft',{'text':'Saved','photos':[],'revision':revision}).status_code,200)

    def test_reject_invalid_photo(self):
        r = self.client.post('/api/photos', data={'photo':(io.BytesIO(b'<script>bad</script>'),'x.jpg')},headers=self.headers)
        self.assertEqual(r.status_code,400)

    def test_schedule_validation(self):
        g = self.group()
        b = {'mode':'api','enabled':True,'interval_hours':24,'next_at':time.time()+3600}
        self.assertEqual(self.api('PUT',f"groups/{g['id']}",b).status_code,400)
        b['enabled']=False
        self.assertEqual(self.api('PUT',f"groups/{g['id']}",b).status_code,200)
        for hours in (0,-1,float('nan'),float('inf'),10000):
            self.assertEqual(self.api('PUT',f"groups/{g['id']}",{**b,'interval_hours':hours}).status_code,400)

    def test_duplicate_claim_and_success(self):
        g = self.ready()
        task = self.store.claim(g['id'])
        with self.assertRaises(ValueError):
            self.store.claim(g['id'])
        fake = MagicMock()
        fake.return_value.post.return_value = 'https://vk.com/wall-123_99'
        self.store.deliver(task,fake)
        self.assertEqual(self.store.state['history'][0]['status'],'sent')
        fake.return_value.post.assert_called_once()
        self.assertGreater(g['next_at'],time.time())
        self.assertTrue(g['enabled'])

    def test_uncertain_delivery_pauses_no_automatic_retry(self):
        g = self.ready()
        task = self.store.claim(g['id'])
        fake = MagicMock()
        fake.return_value.post.side_effect = UncertainDelivery('Check VK')
        self.store.deliver(task,fake)
        self.assertFalse(g['enabled'])
        self.assertEqual(self.store.state['history'][0]['status'],'unknown')
        with patch.object(self.store,'deliver') as deliver:
            self.store.tick()
            deliver.assert_not_called()

    def test_restart_recovers_inflight(self):
        g = self.ready()
        self.store.claim(g['id'])
        recovered = Store(self.temp.name)
        recovered.recover()
        self.assertEqual(recovered.state['history'][0]['status'],'unknown')
        self.assertFalse(recovered.group(g['id'])['enabled'])

    def test_missed_intervals_single_send(self):
        g = self.group();g.update(mode='browser_suggest',browser_checked='browser_suggest',enabled=True)
        self.store.state['draft']['text']='Scheduled post'
        g['next_at'] = time.time()-10*86400
        def complete(task):
            with patch.object(self.store.browser,'call',return_value={'status':'browser_suggested','url':'','detail':'Accepted'}):
                Store.deliver(self.store,task)
        with patch.object(self.store,'deliver',side_effect=complete) as deliver:
            self.store.tick(); self.store.tick()
            self.assertEqual(deliver.call_count,1)

    def test_browser_scheduler_needs_check_but_not_api_token(self):
        g=self.group();g.update(mode='browser_suggest',enabled=True,next_at=time.time()-1)
        self.store.state['draft']['text']='Browser post'
        with self.assertRaises(ValueError):self.store.claim(g['id'])
        g['browser_checked']='browser_suggest'
        with patch.object(self.store.browser,'call',return_value={'status':'browser_suggested','detail':'Confirmed','url':''}) as browser:
            self.store.tick()
            browser.assert_called_once()
        self.assertEqual(self.store.state['history'][0]['status'],'browser_suggested')
        self.assertEqual(self.store.state['tasks'],[])
        self.assertTrue(g['enabled'])
        self.assertGreater(g['next_at'],time.time())
        with self.assertRaises(ValueError):self.store.prepare(g['id'])

    def test_browser_failure_pauses_and_never_falls_back_to_api(self):
        g=self.group();g.update(mode='browser_suggest',browser_checked='browser_suggest',enabled=True,next_at=time.time()-1)
        self.store.state['draft']['text']='Browser post'
        with patch.object(self.store.browser,'call',side_effect=UncertainDelivery('Check VK')),patch('app.VK') as api:
            self.store.tick();self.store.tick()
            api.assert_not_called()
        self.assertFalse(g['enabled'])
        self.assertEqual(self.store.state['history'][0]['status'],'unknown')

    def test_manual_modes_work_without_token_and_export_snapshot(self):
        import zipfile
        g = self.group()
        self.store.state['draft']['text'] = 'Snapshot'
        stream=io.BytesIO()
        Image.new('RGB',(10,10),'red').save(stream,format='PNG');stream.seek(0)
        upload=self.client.post('/api/photos',data={'photo':(stream,'test.png')},headers=self.headers)
        self.store.state['draft']['photos']=[upload.json['name']]
        g.update(enabled=True,next_at=time.time()-1)
        with patch('app.VK') as vk:
            self.store.prepare(g['id']);self.store.prepare(g['id'])
            vk.assert_not_called()
        self.assertEqual(len(self.store.state['tasks']),1)
        task=self.store.state['tasks'][0]
        self.store.state['draft']['text']='Changed after preparation'
        with self.client.get('/api/tasks/'+task['id']+'/package') as response:
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                self.assertEqual(archive.read('text.txt').decode(),'Snapshot')
                self.assertIn('photos/01.jpg',archive.namelist())
        self.assertEqual(self.api('POST',f"groups/{g['id']}/send",{}).status_code,400)
        self.assertEqual(self.api('POST',f"tasks/{task['id']}/finish",{'result':'done'}).status_code,200)
        self.assertEqual(self.store.state['history'][0]['status'],'manual_posted')
        self.assertGreater(g['next_at'],time.time())
        self.assertEqual(self.api('POST',f"tasks/{task['id']}/finish",{'result':'done'}).status_code,404)

    def test_suggestion_pauses_until_moderation_and_mode_change_blocked(self):
        g=self.group();g['mode']='manual_suggest';g['enabled']=True
        self.store.state['draft']['text']='Test'
        task=self.store.prepare(g['id'])
        result=self.api('PUT',f"groups/{g['id']}",{'mode':'manual_wall','enabled':True,'next_at':time.time()+60,'interval_hours':24})
        self.assertEqual(result.status_code,400)
        recovered=Store(self.temp.name)
        self.assertEqual(len(recovered.state['tasks']),1)
        self.api('POST',f"tasks/{task['id']}/finish",{'result':'done'})
        self.assertFalse(g['enabled'])
        self.assertEqual(self.store.state['history'][0]['status'],'manual_suggested')

    def test_removed_manual_mode_cannot_be_enabled(self):
        g=self.group();self.store.state['draft']['text']='Test'
        result=self.api('PUT',f"groups/{g['id']}",{'mode':'manual_suggest','enabled':True,'next_at':time.time()+60,'interval_hours':24})
        self.assertEqual(result.status_code,400)
        self.assertFalse(g['enabled'])

    def test_automation_stop_preserves_schedules_and_blocks_tick(self):
        g=self.group();g.update(mode='browser_suggest',browser_checked='browser_suggest',enabled=True,next_at=0)
        self.store.state['draft']['text']='Test'
        self.assertTrue(self.client.get('/api/state').json['automation']['running'])
        self.api('POST','automation',{'running':False})
        with patch.object(self.store,'deliver') as deliver:
            self.store.tick();deliver.assert_not_called()
        self.assertIsNone(self.store.claim(g['id'], scheduled=True))
        self.assertTrue(g['enabled'])
        self.assertFalse(self.client.get('/api/state').json['automation']['running'])
        self.assertTrue(Store(self.temp.name).state['automation_paused'])
        self.api('POST','automation',{'running':True})
        self.assertTrue(self.client.get('/api/state').json['automation']['running'])

    def test_probe_does_not_send_and_records_failure(self):
        g=self.group();g['mode']='browser_suggest';self.store.state['draft']['text']='Test'
        with patch.object(self.store.browser,'call',return_value={'composer_found':True,'message':'Ready'}) as browser:
            r=self.api('POST',f"groups/{g['id']}/browser-check",{})
            self.assertEqual(r.status_code,200)
            self.assertEqual(browser.call_args.args[0],'inspect')
            self.assertEqual(browser.call_args.args[2]['text'],'Test')
        self.assertEqual(g['browser_checked'],'browser_suggest')
        self.assertFalse(self.store.busy)
        with patch.object(self.store.browser,'call',side_effect=VKError('No editor')):
            self.assertEqual(self.api('POST',f"groups/{g['id']}/browser-check",{}).status_code,400)
        self.assertNotIn('browser_checked',g)
        self.assertEqual(g['browser_error'],'No editor')
        self.assertFalse(self.store.busy)

    def test_token_never_returned_or_saved(self):
        with patch('app.VK') as vk:
            vk.return_value.identity.return_value={'id':123,'name':'Test'}
            r=self.api('POST','connect',{'token':'private-secret-for-test'})
            self.assertEqual(r.status_code,200)
        self.assertNotIn('private-secret-for-test',self.client.get('/api/state').text)
        self.assertNotIn('private-secret-for-test',self.store.file.read_text())
        self.api('POST','disconnect',{})
        self.assertFalse(self.store.token)

    def test_pausing_during_send_preserves_pause(self):
        g = self.ready()
        task=self.store.claim(g['id'])
        self.api('POST','pause',{})
        fake=MagicMock();fake.return_value.post.return_value='https://vk.com/wall-123_1'
        self.store.deliver(task,fake)
        self.assertFalse(g['enabled'])


class TransportTests(unittest.TestCase):
    @patch('vk_api.time.sleep')
    @patch('vk_api.requests.post')
    def test_error_redacted(self,post,sleep):
        post.return_value.json.return_value={'error':{'error_code':15,'error_msg':'secret-token'}}
        with self.assertRaises(VKError) as caught:
            VK('secret-token').call('wall.post')
        self.assertNotIn('secret-token',str(caught.exception))

    @patch('vk_api.time.sleep')
    @patch('vk_api.requests.post')
    def test_timeout_is_unknown_and_not_retried(self,post,sleep):
        post.side_effect=requests.Timeout('contains sensitive details')
        with self.assertRaises(UncertainDelivery):
            VK('secret').post(123,'test',[],'fixed-job-id')
        self.assertEqual(post.call_count,1)

    @patch('vk_api.VK.call')
    def test_posts_from_user_and_negative_owner(self,call):
        call.return_value={'post_id':42}
        url=VK('secret').post(123,'text',['photo1_2'],'job')
        self.assertEqual(url,'https://vk.com/wall-123_42')
        self.assertEqual(call.call_args.kwargs['owner_id'],-123)
        self.assertEqual(call.call_args.kwargs['from_group'],0)


if __name__ == '__main__':
    unittest.main()
