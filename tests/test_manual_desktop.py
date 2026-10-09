import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from browser_vk import BrowserVK


class ManualDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.b=BrowserVK(self.tmp.name)
    def tearDown(self):
        self.b.executor.shutdown()
        self.tmp.cleanup()

    def test_connect_never_attaches_controller(self):
        with patch.dict('os.environ',{'VK_BROWSER_DESKTOP':'1'}), patch.object(self.b,'_native_window') as native, patch.object(self.b,'_ensure') as control:
            self.b._connect()
            native.assert_called_once()
            control.assert_not_called()
            self.assertIsNone(self.b.context)
            self.assertIsNone(self.b.playwright)
            self.assertFalse(self.b.authenticated)

    def test_detach_does_not_close_context_or_chrome(self):
        context=Mock();desktop=Mock();pw=Mock()
        self.b.context=context;self.b.desktop=desktop;self.b.playwright=pw
        self.b._detach_control()
        context.close.assert_not_called();desktop.close.assert_not_called()
        pw.stop.assert_called_once()
        self.assertIsNone(self.b.context)
        self.assertIs(self.b.desktop,desktop)

    def test_heartbeat_never_inspects_vk_page(self):
        self.b.desktop=Mock(screen_port=12345,screen_until=0)
        with patch.object(self.b,'_ensure') as ensure,patch.object(self.b,'_auth_evidence') as read:
            result=self.b._desktop_heartbeat()
            ensure.assert_not_called();read.assert_not_called()
            self.assertEqual(result['state'],'manual')
            self.assertGreater(self.b.desktop.screen_until,time.monotonic())

    def test_verify_does_not_reload_visible_captcha(self):
        page=Mock();self.b.desktop=Mock()
        with patch.object(self.b,'_ensure'),patch.object(self.b,'_login_page',return_value=page),patch.object(self.b,'_auth_evidence',return_value={'challenge':True}),patch.object(self.b,'_detach_control') as detach:
            result=self.b._desktop_verify()
            self.assertEqual(result['state'],'manual')
            page.reload.assert_not_called();detach.assert_called_once()

    def test_explicit_verify_performs_only_one_reload(self):
        page=Mock();self.b.desktop=Mock()
        with patch.object(self.b,'_ensure') as ensure,patch.object(self.b,'_login_page',return_value=page),patch.object(self.b,'_auth_evidence',return_value={}),patch.object(self.b,'_login_progress',return_value={'state':'waiting'}):
            self.b._desktop_verify()
            ensure.assert_called_once();page.reload.assert_called_once()
