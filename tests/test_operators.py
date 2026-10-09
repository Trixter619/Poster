import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from operators import create_gateway


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_gateway(self.tmp.name)
        self.app.testing = True
        self.admin = self.app.test_client()
        self.password = 'Only-test-password-2026'
        self.submit(self.admin, '/setup', username='owner', password=self.password)

    def tearDown(self):
        self.app.workspaces.pool.shutdown()
        self.tmp.cleanup()

    def csrf(self, client, path='/account'):
        text = client.get(path).text
        return re.search(r'name="csrf" value="([^"]+)"', text)[1]

    def submit(self, client, path, **fields):
        fields['csrf'] = self.csrf(client, path)
        return client.post(path, data=fields)

    def second(self):
        self.submit(self.admin, '/account', action='add', username='second', password=self.password)
        c = self.app.test_client()
        self.submit(c, '/login', username='second', password=self.password)
        return c

    def api_headers(self, client):
        return {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]}

    def test_login_required_for_api_photos_and_mutations(self):
        anon = self.app.test_client()
        for path in ('/api/state', '/photos/test.jpg'):
            self.assertEqual(anon.get(path).status_code, 401)
        self.assertEqual(anon.post('/api/groups', json={'url':'club123'}).status_code, 401)
        self.assertEqual(anon.get('/').status_code, 302)
        self.assertEqual(anon.get('/api/health').status_code, 200)

    def test_accounts_have_separate_drafts_groups_photos_and_profiles(self):
        second = self.second()
        headers = self.api_headers(self.admin)
        self.admin.post('/api/groups', json={'url':'club123'}, headers=headers)
        self.admin.put('/api/draft', json={'text':'private owner text','photos':[], 'revision':0}, headers=headers)
        photo = io.BytesIO(); Image.new('RGB',(20,20),'red').save(photo,format='PNG');photo.seek(0)
        upload = self.admin.post('/api/photos', data={'photo':(photo,'x.png')},headers=headers)
        self.assertEqual(upload.status_code,200)
        name = upload.json['name']
        self.assertEqual(second.get('/photos/'+name).status_code,404)
        own = second.get('/api/state').json
        self.assertEqual(own['groups'],[])
        self.assertEqual(own['draft']['text'],'')
        self.assertEqual(own['history'],[])
        apps = [self.app.workspaces.get(u) for u in self.app.operators.users()]
        self.assertNotEqual(apps[0].store.browser.folder, apps[1].store.browser.folder)
        self.assertEqual(apps[0].store.folder,Path(self.tmp.name).resolve())
        self.assertIs(apps[0].store.browser.pool,apps[1].store.browser.pool)
        self.assertEqual(second.post('/api/groups',json={'url':'club456'},headers=headers).status_code,403)

    def test_operator_cannot_create_users_or_disable_owner(self):
        second = self.second()
        response = self.submit(second,'/account',action='add',username='third',password=self.password)
        self.assertEqual(response.status_code,403)
        self.assertEqual(len(self.app.operators.users()),2)

    def test_disable_revokes_session_and_stops_schedule(self):
        second = self.second()
        uid = self.app.operators.users()[1]['id']
        tenant = self.app.workspaces.get(self.app.operators.user(uid))
        self.assertTrue(tenant.store.operator_active())
        self.submit(self.admin,'/account',action='toggle',uid=uid)
        self.assertEqual(second.get('/api/state').status_code,401)
        self.assertFalse(tenant.store.operator_active())
        with patch.object(tenant.store,'tick') as tick:
            self.app.workspaces.tick()
            tick.assert_not_called()

    def test_logout_invalidates_replayed_cookie(self):
        cookie = self.admin.get_cookie('vkposter_session').value
        token = self.csrf(self.admin)
        self.admin.post('/logout',data={'csrf':token})
        self.admin.set_cookie('vkposter_session',cookie)
        self.assertEqual(self.admin.get('/api/state').status_code,401)

    def test_csrf_and_password_change_revoke_other_sessions(self):
        self.assertEqual(self.admin.post('/account',data={'action':'add'}).status_code,403)
        csrf = self.csrf(self.admin)
        self.assertEqual(self.admin.post('/account', data={'csrf':csrf,'action':'add'}, headers={'Origin':'https://evil.example'}).status_code,403)
        other = self.app.test_client()
        self.submit(other,'/login',username='owner',password=self.password)
        response = self.submit(self.admin,'/account',action='password',old_password=self.password,password='Changed-test-password-2026',password_confirm='Changed-test-password-2026')
        self.assertEqual(response.status_code,302)
        self.assertEqual(other.get('/api/state').status_code,401)

    def test_no_environment_token_leaks_to_operator(self):
        with patch.dict('os.environ',{'VK_ACCESS_TOKEN':'test-only-not-secret'}):
            self.admin.get('/')
        user = self.app.operators.users()[0]
        self.assertEqual(self.app.workspaces.get(user).store.token,'')

    def test_setup_cannot_be_repeated_and_auth_is_persistent(self):
        self.assertEqual(self.admin.get('/setup').status_code,302)
        restarted = create_gateway(self.tmp.name)
        self.assertEqual(len(restarted.operators.users()),1)
        self.assertIsNotNone(restarted.operators.authenticate('owner',self.password,'127.0.0.1'))
        restarted.workspaces.pool.shutdown()

    def test_public_request_needs_owner_approval_and_records_actor(self):
        candidate = self.app.test_client()
        response = self.submit(candidate, '/register', username='candidate', password=self.password, admin='1', active='1')
        self.assertIn('Заявка отправлена',response.text)
        user = self.app.operators.users()[-1]
        self.assertFalse(user['active'])
        self.assertFalse(user['admin'])
        self.assertEqual(user['approval'],'pending')
        self.assertNotIn(user['id'],self.app.workspaces.apps)
        self.assertIn('ожидает одобрения',self.submit(candidate,'/login',username='candidate',password=self.password).text)
        self.assertEqual(candidate.get('/api/state').status_code,401)
        other = self.second()
        self.assertEqual(self.submit(other,'/account',action='approve',uid=user['id']).status_code,403)
        self.submit(self.admin,'/account',action='toggle',uid=user['id'])
        self.assertFalse(self.app.operators.user(user['id'])['active'])
        self.submit(self.admin,'/account',action='approve',uid=user['id'])
        self.assertEqual(self.app.operators.user(user['id'])['approval'],'approved')
        self.submit(candidate,'/login',username='candidate',password=self.password)
        self.assertEqual(candidate.get('/api/state').status_code,200)
        event = next(e for e in self.app.operators.audit() if e['action']=='approve')
        self.assertEqual(event['actor'],'owner')
        self.assertEqual(event['target'],'candidate')

    def test_rejected_request_and_registration_rate_limit(self):
        candidate=self.app.test_client()
        self.submit(candidate,'/register',username='rejected',password=self.password)
        uid=self.app.operators.users()[-1]['id']
        self.submit(self.admin,'/account',action='reject',uid=uid)
        self.assertEqual(self.app.operators.user(uid)['approval'],'rejected')
        with self.assertRaises(ValueError):self.app.operators.authenticate('rejected',self.password,'test')
        with self.assertRaises(ValueError):self.app.operators.decide(uid,self.app.operators.users()[0]['id'],True)
        for _ in range(5):self.app.operators.registration_limit('limit-test')
        with self.assertRaises(ValueError):self.app.operators.registration_limit('limit-test')

    def test_public_https_proxy_login_tenant_and_setup_block(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'VK_POSTER_PUBLIC_HOST':'158.160.217.50','VK_POSTER_PROXY':'1','VK_POSTER_HTTPS':'1'}):
            app=create_gateway(tmp)
            try:
                app.operators.add('owner',self.password,first=True)
                client=app.test_client()
                headers={'Host':'158.160.217.50','X-Forwarded-Proto':'https','X-Forwarded-For':'192.0.2.12'}
                response=client.get('/login',headers=headers)
                self.assertIn('Secure;',response.headers['Set-Cookie'])
                token=re.search(r'name="csrf" value="([^"]+)"',response.text)[1]
                headers['Origin']='https://158.160.217.50'
                response=client.post('/login',headers=headers,data={'csrf':token,'username':'owner','password':self.password})
                self.assertEqual(response.status_code,302)
                self.assertEqual(client.get('/api/state',headers=headers).status_code,200)
                self.assertEqual(client.get('/setup',headers=headers).status_code,403)
            finally:app.workspaces.pool.shutdown()

    def test_rate_limit(self):
        # Validate persisted rate limiter without repeated costly password hashing.
        with patch('operators.check_password_hash', return_value=False):
            for _ in range(10):
                with self.assertRaises(ValueError):
                    self.app.operators.authenticate('bad','bad','test-ip')
            with self.assertRaisesRegex(ValueError,'Слишком много'):
                self.app.operators.authenticate('owner',self.password,'test-ip')
