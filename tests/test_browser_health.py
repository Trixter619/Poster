import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from browser_vk import BrowserVK


class HealthTests(unittest.TestCase):
    def test_health_contains_only_status_and_boolean_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'VK_BROWSER_HEALTH':'1'}):
            browser=BrowserVK(tmp)
            try:
                browser.auth_api_state='rejected'
                browser._health('waiting',{'signedIn':True,'loading':True,'password':'secret-test','url':'private-test','id':123})
                text=(Path(tmp)/'vk-health.json').read_text()
                result=json.loads(text)['events'][0]
                self.assertEqual(set(result),{'at','stage','probe','evidence'})
                self.assertEqual(set(result['evidence']),{'signedIn','menu','loginForm','loading'})
                self.assertNotIn('secret-test',text)
                self.assertNotIn('private-test',text)
                self.assertEqual((Path(tmp)/'vk-health.json').stat().st_mode & 0o777,0o600)
            finally:browser.executor.shutdown()

    def test_health_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'VK_BROWSER_HEALTH':'0'}):
            browser=BrowserVK(tmp)
            try:
                browser._health('opened')
                self.assertFalse((Path(tmp)/'vk-health.json').exists())
            finally:browser.executor.shutdown()

    def test_repeated_rejections_preserve_login_events(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'VK_BROWSER_HEALTH':'1'}):
            browser=BrowserVK(tmp)
            try:
                browser._health('opened')
                browser.auth_api_state='rejected'
                for _ in range(50):browser._health('vk_rejected')
                browser._health('waiting',{'loading':True})
                events=json.loads((Path(tmp)/'vk-health.json').read_text())['events']
                self.assertEqual([e['stage'] for e in events],['opened','vk_rejected','waiting'])
                self.assertEqual(events[1]['count'],50)
            finally:browser.executor.shutdown()

    def test_qr_diagnostics_never_record_auth_response_values(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'VK_BROWSER_HEALTH':'1'}):
            browser=BrowserVK(tmp)
            try:
                response=Mock(url='https://api.vk.ru/method/auth.checkAuthCode?code=hidden')
                response.json.return_value={'response':{'token':'secret-token','code':'secret-code'}}
                browser._observe_auth_response(response)
                text=(Path(tmp)/'vk-health.json').read_text()
                self.assertIn('auth.checkAuthCode:response',text)
                self.assertNotIn('secret-',text)
                self.assertNotIn('hidden',text)
                self.assertIsNone(browser.auth_api_state)
                self.assertFalse(browser.authenticated)
            finally:browser.executor.shutdown()
