"""Auth detection without extracting account identifiers or sending posts."""
import tempfile
import unittest
from unittest.mock import patch, Mock
from playwright.sync_api import sync_playwright
from browser_vk import BrowserVK
from vk_api import VKError


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vk = BrowserVK(self.tmp.name)
        self.page = self.browser.new_page()
        self.page.route('https://vk.ru/**', lambda r: r.fulfill(body='<html><body></body></html>', content_type='text/html'))
        self.page.goto('https://vk.ru/')

    def tearDown(self):
        self.page.close()
        self.vk.executor.shutdown()
        self.tmp.cleanup()

    def test_viewer_bootstrap_recognized_before_menu(self):
        self.page.evaluate('window.vk={id:123};document.body.innerHTML=\'<div data-testid="loading-skeleton">Загрузка</div>\'')
        self.vk.auth_api_state = 'accepted'
        with self.assertRaises(VKError):self.vk._confirm_login(self.page, timeout=0)
        self.page.evaluate('document.body.innerHTML=""')
        self.vk._confirm_login(self.page, timeout=0)
        self.assertTrue(self.vk.authenticated)
        self.assertNotIn('123', str(self.vk.info()))

    def test_recognized_viewer_with_rejected_api_never_confirms(self):
        self.page.evaluate("window.vk={id:123};document.body.innerHTML='<a id=\"top_profile_link\">Профиль</a>'")
        self.vk.auth_api_state = 'rejected'
        with self.assertRaisesRegex(VKError, 'авторизации 5'):
            self.vk._confirm_login(self.page, timeout=0)
        self.assertFalse(self.vk.authenticated)

    def test_bootstrap_alone_is_not_a_working_session(self):
        self.page.evaluate('window.vk={id:123}')
        with self.assertRaises(VKError):
            self.vk._confirm_login(self.page, timeout=0)

    def test_observer_records_only_account_probe_status(self):
        response = Mock(url='https://web.api.vk.ru/method/account.getInfo')
        response.json.return_value = {'error': {'error_code': 5}}
        self.vk._observe_auth_response(response)
        self.assertEqual(self.vk.auth_api_state, 'rejected')
        response.json.return_value = {'response': {}}
        self.vk._observe_auth_response(response)
        self.assertEqual(self.vk.auth_api_state, 'accepted')
        response.url = 'https://web.api.vk.ru/method/wall.get'
        response.json.return_value = {'error': {'error_code': 5}}
        self.vk._observe_auth_response(response)
        self.assertEqual(self.vk.auth_api_state, 'accepted')

    def test_reconnect_clears_only_own_context(self):
        self.vk.context = Mock()
        self.vk.context.pages = [self.page]
        self.vk.page = self.page
        with patch.object(self.vk, '_ensure'), patch.object(self.vk, '_open', return_value={}) as opened:
            self.vk._reconnect()
        self.vk.context.clear_cookies.assert_called_once_with()
        opened.assert_called_once_with()
        self.assertFalse(self.vk.authenticated)

    def test_rejected_probe_does_not_interrupt_login_or_clear_session(self):
        self.vk.auth_api_state = 'rejected'
        self.page.set_content('<input type="tel">')
        with patch.object(self.vk, '_login_page', return_value=self.page), patch.object(self.vk, '_reconnect') as repair:
            for _ in range(3):
                self.assertEqual(self.vk._login_progress()['state'], 'waiting')
            repair.assert_not_called()
        self.assertFalse(self.vk.authenticated)

    def test_stalled_page_offers_reload_without_reset_or_close(self):
        import time
        self.vk.login_started=time.monotonic()-60
        self.vk.auth_api_state='rejected'
        with patch.object(self.vk, '_login_page', return_value=self.page), patch.object(self.vk, '_reconnect') as repair, patch.object(self.vk, '_close') as close:
            self.assertEqual(self.vk._login_progress()['state'], 'session_rejected')
            repair.assert_not_called();close.assert_not_called()

    def test_healthy_session_completes_without_reset(self):
        import time
        self.vk.login_confirmed_at=time.monotonic()-4
        self.page.set_content('<a id="top_profile_link">Профиль</a>')
        with patch.object(self.vk, '_login_page', return_value=self.page), patch.object(self.vk, '_reconnect') as repair:
            self.assertEqual(self.vk._login_progress()['state'], 'connected')
            repair.assert_not_called()

    def test_zero_viewer_with_login_form_requires_login(self):
        self.page.evaluate('window.vk={id:0};document.body.innerHTML=\'<input type="tel">\'')
        with self.assertRaisesRegex(VKError, 'страницу входа'):
            self.vk._confirm_login(self.page, timeout=0)
        self.assertFalse(self.vk.authenticated)
        self.assertFalse(self.page.is_closed())

    def test_loading_is_distinguished_from_logged_out(self):
        self.page.set_content('<div data-testid="loading-skeleton">Загрузка</div>')
        with self.assertRaisesRegex(VKError, 'загрузке интерфейса'):
            self.vk._confirm_login(self.page, timeout=0)

    def test_legacy_visible_menu_supported(self):
        self.page.set_content('<a id="top_profile_link">Профиль</a>')
        self.vk._confirm_login(self.page, timeout=0)
        self.assertTrue(self.vk.authenticated)

    def test_hidden_menu_does_not_confirm_auth(self):
        self.page.set_content('<a id="top_profile_link" hidden>Профиль</a>')
        with self.assertRaises(VKError):
            self.vk._confirm_login(self.page, timeout=0)

    def test_non_vk_page_cannot_confirm_auth(self):
        self.page.goto('about:blank')
        self.page.evaluate('window.vk={id:123}')
        with self.assertRaises(VKError):
            self.vk._confirm_login(self.page, timeout=0)

    def test_login_must_remain_valid_before_closing_remote_view(self):
        self.page.set_content('<a id="top_profile_link">Профиль</a>')
        with patch.object(self.vk, '_login_page', return_value=self.page):
            self.assertEqual(self.vk._login_progress()['state'],'waiting')
            self.assertFalse(self.vk.authenticated)
            self.vk.auth_api_state='rejected'
            self.assertNotEqual(self.vk._login_progress()['state'],'connected')
            self.assertIsNone(self.vk.login_confirmed_at)

    def test_explicit_reconnect_clears_only_own_site_storage(self):
        self.page.evaluate("localStorage.setItem('test-session','old')")
        other=self.browser.new_page()
        other.route('https://vk.ru/**',lambda r:r.fulfill(body='<html>other</html>'))
        other.goto('https://vk.ru/')
        other.evaluate("localStorage.setItem('test-session','keep')")
        self.vk.context=self.page.context
        self.vk.page=self.page
        try:
            with patch.object(self.vk,'_ensure'),patch.object(self.vk,'_open',return_value={}):
                self.vk._reconnect()
            self.page.goto('https://vk.ru/')
            self.assertIsNone(self.page.evaluate("localStorage.getItem('test-session')"))
            self.assertEqual(other.evaluate("localStorage.getItem('test-session')"),'keep')
        finally:other.close()
