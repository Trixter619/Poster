import base64
import hashlib
import re
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch
from operators import create_gateway
from vkid_auth import AuthFailure


class VKIDTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_gateway(self.tmp.name)
        self.app.config.update(TESTING=True, TRUSTED_HOSTS=['sagposter.duckdns.org', 'localhost'])
        self.uid = self.app.operators.add('testowner', 'Only-test-password-2026', first=True)
        self.user = self.app.operators.user(self.uid)
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url='https://sagposter.duckdns.org') as s:
            s.update(uid=self.uid, version=1, csrf='test-csrf')

    def tearDown(self):
        self.app.workspaces.pool.shutdown()
        self.tmp.cleanup()

    def start(self):
        r = self.client.post('/auth/vk/start', base_url='https://sagposter.duckdns.org', data={'csrf':'test-csrf'})
        self.assertEqual(r.status_code, 303)
        return parse_qs(urlsplit(r.location).query)

    def callback(self, q):
        return self.client.get('/auth/vk/callback', base_url='https://sagposter.duckdns.org',
             query_string={'state':q['state'][0], 'code':'test-code', 'device_id':'test-device'})

    def response(self, state, **extra):
        return Mock(json=Mock(return_value=dict(state=state, access_token='TEST-ACCESS-ONLY',
            refresh_token='TEST-REFRESH-ONLY', user_id=12345, expires_in=3600, scope='vkid.personal_info', **extra)))

    @patch('vkid_auth.requests.post')
    def test_pkce_exchange_saved_and_secrets_never_rendered(self, post):
        q = self.start()
        self.assertEqual(q['client_id'], ['54800785'])
        self.assertEqual(q['scope'], ['wall photos'])
        self.assertEqual(q['prompt'], ['consent'])
        self.assertEqual(q['redirect_uri'], ['https://sagposter.duckdns.org/auth/vk/callback'])
        post.return_value = self.response(q['state'][0])
        r = self.callback(q)
        self.assertEqual(r.location, '/vk-id')
        self.assertEqual(r.headers['Referrer-Policy'], 'no-referrer')
        kwargs = post.call_args.kwargs
        verifier = kwargs['data']['code_verifier']
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge, q['code_challenge'][0])
        self.assertFalse(kwargs['allow_redirects'])
        page = self.client.get('/vk-id', base_url='https://sagposter.duckdns.org').text
        self.assertIn('VK ID подключён', page)
        self.assertIn('12345', page)
        for secret in ['TEST-ACCESS-ONLY','TEST-REFRESH-ONLY',verifier,'test-device']:
            self.assertNotIn(secret, page)
        self.assertEqual(set(self.app.vkid.status(self.uid)), {'user_id','scope','expired'})
        self.callback(q)
        self.assertEqual(post.call_count, 1)

    @patch('vkid_auth.requests.post')
    def test_bad_state_expired_and_cross_operator_do_not_exchange(self, post):
        q = self.start()
        with self.client.session_transaction(base_url='https://sagposter.duckdns.org') as s:
            s['vkid_binding'] = 'another-browser'
        self.callback(q)
        post.assert_not_called()
        q = self.start()
        with self.app.operators.connect() as db:
            db.execute('UPDATE vkid_flows SET expires=0')
        self.callback(q)
        post.assert_not_called()
        other = self.app.operators.add('otheruser', 'Only-test-password-2026')
        q = self.start()
        with self.client.session_transaction(base_url='https://sagposter.duckdns.org') as s:
            s['uid'] = other
        self.callback(q)
        post.assert_not_called()
        self.assertIsNone(self.app.vkid.status(other))

    @patch('vkid_auth.requests.post')
    def test_mismatched_response_and_revoked_session_not_saved(self, post):
        q = self.start(); post.return_value = self.response('incorrect-state')
        self.callback(q)
        self.assertIsNone(self.app.vkid.status(self.uid))
        q = self.start()
        def revoke(*a, **kw):
            self.app.operators.invalidate(self.uid)
            return self.response(q['state'][0])
        post.side_effect = revoke
        self.callback(q)
        self.assertIsNone(self.app.vkid.status(self.uid))

    def test_csrf_required_and_guest_cannot_connect(self):
        r = self.client.post('/auth/vk/start', base_url='https://sagposter.duckdns.org')
        self.assertEqual(r.status_code, 403)
        guest = self.app.test_client()
        self.assertEqual(guest.get('/vk-id').location, '/login')
        self.assertEqual(self.app.config['SESSION_COOKIE_SAMESITE'], 'Lax')
        self.assertEqual(self.app.vkid.status(self.uid), None)
