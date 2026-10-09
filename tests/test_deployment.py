import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from operators import create_gateway


class DeploymentTests(unittest.TestCase):
    def test_health_is_public_but_private_api_requires_login(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_gateway(folder)
            try:
                client=app.test_client()
                self.assertEqual(client.get('/api/health').status_code,200)
                self.assertEqual(client.get('/api/state').status_code,401)
            finally:app.workspaces.pool.shutdown()

    def test_oauth_uses_configured_domain_and_client(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_gateway(folder);app.config['TRUSTED_HOSTS']=['poster.example.com']
            try:
                uid=app.operators.add('owner','Deployment-test-password',first=True)
                client=app.test_client()
                with client.session_transaction(base_url='https://poster.example.com') as session:
                    session.update(uid=uid,version=1,csrf='test-csrf')
                with patch('vkid_auth.APP_ID','123456'),patch('vkid_auth.CALLBACK','https://poster.example.com/auth/vk/callback'),patch('vkid_auth.CALLBACK_ORIGIN','https://poster.example.com'):
                    response=client.post('/auth/vk/start',base_url='https://poster.example.com',data={'csrf':'test-csrf'})
                    params=parse_qs(urlsplit(response.location).query)
                    self.assertEqual(params['client_id'],['123456'])
                    self.assertEqual(params['redirect_uri'],['https://poster.example.com/auth/vk/callback'])
                    self.assertNotIn('sagposter',client.get('/vk-id',base_url='https://poster.example.com').text)
            finally:app.workspaces.pool.shutdown()
