import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from app import create_app


class ApiOnlyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'VK_POSTER_LEGACY_BROWSER':'0'})
        env.start(); self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = create_app(self.tmp.name)
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]}

    def test_no_browser_module_or_worker_is_loaded(self):
        code = "import app,operators,sys,tempfile; a=operators.create_gateway(tempfile.mkdtemp()); assert 'browser_vk' not in sys.modules; assert 'playwright' not in sys.modules; assert a.workspaces.pool.info()['limit']==0; a.workspaces.pool.shutdown()"
        subprocess.run([sys.executable,'-c',code],check=True,env=dict(os.environ,VK_POSTER_LEGACY_BROWSER='0'),stdout=subprocess.DEVNULL)
        self.assertFalse((Path(self.tmp.name)/'vk-browser').exists())

    def test_browser_routes_and_old_token_flow_are_unavailable(self):
        for action in ['open','connect','connect-status','finish','reconnect','screen','input']:
            self.assertEqual(self.client.post('/api/browser/'+action,json={},headers=self.headers).status_code,410)
        self.assertEqual(self.client.post('/api/connect',json={},headers=self.headers).status_code,409)
        self.assertNotIn('remoteImage',self.client.get('/vk-login').text)
        self.assertIn('Ожидается разрешение VK',self.client.get('/vk-connect').text)

    def test_migration_keeps_settings_and_history_without_running_jobs(self):
        store=self.app.store
        group={'id':'g','name':'test','slug':'club123','url':'https://vk.ru/club123','mode':'browser_suggest','enabled':True,'interval_hours':48,'next_at':time.time()-1}
        watch={'id':'h','group_id':'g','status':'unknown','publication':{'state':'pending','next_check':0}}
        store.state.update(groups=[group],history=[watch],draft={'text':'keep me','photos':[]})
        store.recover()
        self.assertTrue(group['enabled']);self.assertEqual(group['interval_hours'],48)
        self.assertEqual(group['mode'],'api');self.assertEqual(group['previous_transport'],'browser_suggest')
        self.assertEqual(watch['publication']['state'],'pending')
        self.assertFalse(store.automation_info()['running'])
        with patch.object(store,'deliver',side_effect=AssertionError('must not send')),patch.object(store,'check_publication',side_effect=AssertionError('must not check')):
            store.tick()
        with self.assertRaisesRegex(ValueError,'VK API'):store.claim('g')
        with self.assertRaisesRegex(ValueError,'VK API'):store.claim_publication('h')
        self.assertEqual(self.client.post('/api/groups/g/send',json={},headers=self.headers).status_code,409)

    def test_editing_content_and_intervals_still_works(self):
        result=self.client.post('/api/groups',json={'url':'club123'},headers=self.headers)
        gid=result.json['id']
        payload={'mode':'api','enabled':False,'interval_hours':48,'next_at':time.time()+86400}
        self.assertEqual(self.client.put('/api/groups/'+gid,json=payload,headers=self.headers).status_code,200)
        payload['enabled']=True
        self.assertEqual(self.client.put('/api/groups/'+gid,json=payload,headers=self.headers).status_code,400)
        self.assertEqual(self.client.put('/api/draft',json={'text':'Объявление','photos':[],'revision':0},headers=self.headers).status_code,200)
        state=self.client.get('/api/state').json
        self.assertEqual(state['draft']['text'],'Объявление')
        self.assertTrue(state['integration']['browser_removed'])
        self.assertFalse(state['integration']['ready'])
        self.assertEqual(self.client.post('/api/automation',json={'running':True},headers=self.headers).status_code,409)

    def test_return_to_browser_preserves_pause_and_schedule(self):
        store=self.app.store
        store.state.update(automation_paused=True,groups=[{'id':'g','mode':'api','previous_transport':'browser_suggest','enabled':True,'interval_hours':48,'next_at':123,'browser_checked':{'ok':True}}])
        store.api_only=False
        store.recover()
        group=store.state['groups'][0]
        self.assertEqual(group['mode'],'browser_suggest')
        self.assertTrue(group['enabled'])
        self.assertEqual(group['interval_hours'],48)
        self.assertEqual(group['next_at'],123)
        self.assertNotIn('browser_checked',group)
        self.assertTrue(store.state['automation_paused'])
