from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from browser_pool import BrowserPool, BrowserBusy
from browser_vk import BrowserVK
from vk_api import VKError


class FakeBrowser:
    def __init__(self):self.opened=False;self.closed=0
    def _open(self):self.opened=True;return self.info()
    def _work(self):self.opened=True;time.sleep(.01);return 'done'
    def _close(self):self.opened=False;self.closed+=1
    def info(self):return {'opened':self.opened}


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.pool=BrowserPool();self.a=FakeBrowser();self.b=FakeBrowser()
    def tearDown(self):self.pool.shutdown()

    def test_operators_execute_in_parallel_on_different_threads(self):
        barrier=threading.Barrier(2);threads=set()
        def work():threads.add(threading.get_ident());barrier.wait(timeout=2);return 'done'
        self.a._work=work;self.b._work=work
        with ThreadPoolExecutor(2) as clients:
            jobs=[clients.submit(self.pool.call,b,'work') for b in (self.a,self.b)]
            self.assertEqual([j.result() for j in jobs],['done','done'])
        self.assertEqual(len(threads),2)

    def test_single_operator_actions_still_serialized_without_queue_cap(self):
        threads=set()
        def work():threads.add(threading.get_ident());time.sleep(.01)
        self.a._work=work
        with ThreadPoolExecutor(12) as clients:
            jobs=[clients.submit(self.pool.call,self.a,'work') for _ in range(12)]
            for j in jobs:j.result()
        self.assertEqual(len(threads),1)
        self.assertEqual(self.pool.info()['queued'],0)

    def test_login_only_blocks_own_publishing(self):
        self.pool.call(self.a,'open')
        with self.assertRaises(BrowserBusy):self.pool.call(self.a,'work')
        self.assertEqual(self.pool.call(self.b,'work'),'done')
        with self.assertRaises(BrowserBusy):self.pool.call(self.b,'remote_screen')
        self.pool.call(self.b,'open')
        self.assertTrue(self.a.opened);self.assertTrue(self.b.opened)

    def test_success_keeps_browser_warm_and_revokes_remote_control(self):
        self.pool.call(self.a,'open')
        self.a._login_progress=lambda:{'state':'connected'}
        self.pool.call(self.a,'login_progress')
        self.assertTrue(self.a.opened);self.assertTrue(self.pool.available(self.a))
        with self.assertRaises(BrowserBusy):self.pool.call(self.a,'remote_screen')
        self.assertEqual(self.pool.call(self.a,'work'),'done')
        self.pool.collect();self.assertTrue(self.a.opened)

    def test_failure_does_not_close_or_unlock_own_browser(self):
        self.pool.call(self.a,'open')
        def fail():raise VKError('not ready')
        self.a._finish_login=fail
        with self.assertRaises(VKError):self.pool.call(self.a,'finish_login')
        self.assertTrue(self.a.opened);self.assertFalse(self.pool.available(self.a))
        self.assertTrue(self.pool.available(self.b))

    def test_close_only_own_and_shutdown_both(self):
        self.pool.call(self.a,'open');self.pool.call(self.b,'open')
        self.pool.call(self.a,'close')
        self.assertFalse(self.a.opened);self.assertTrue(self.b.opened)
        self.pool.shutdown();self.assertFalse(self.b.opened)
        with self.assertRaises(VKError):self.pool.call(self.b,'open')

    def test_expired_login_revokes_screen_but_keeps_browser(self):
        self.pool.call(self.a,'open')
        with self.pool.lock:self.pool.workers[self.a].lease_until=0
        with self.assertRaises(BrowserBusy):self.pool.call(self.a,'remote_screen')
        self.pool.collect();self.assertTrue(self.a.opened)


class RemoteScreenTests(unittest.TestCase):
    def test_independent_screens_fast_capture_and_batched_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'VK_BROWSER_HEADLESS':'1'}):
            pool=BrowserPool();a=BrowserVK(tmp+'/a',pool);b=BrowserVK(tmp+'/b',pool)
            def fixture(browser,color):
                def open_page():
                    browser._ensure()
                    browser.context.route('https://vk.ru/**',lambda r:r.fulfill(body=f'<body style="background:{color}"><input id="field" style="position:absolute;left:0;top:0;width:400px;height:60px">',content_type='text/html'))
                    browser.page.goto('https://vk.ru/');return browser.info()
                browser._open=open_page
            fixture(a,'red');fixture(b,'blue')
            try:
                pool.call(a,'open');pool.call(b,'open')
                first=pool.call(a,'remote_screen');second=pool.call(b,'remote_screen')
                self.assertTrue(first.startswith(b'\xff\xd8'));self.assertNotEqual(first,second)
                pool.call(a,'remote_batch',[{'kind':'click','x':50,'y':30},{'kind':'text','text':'abc'},{'kind':'key','key':'Backspace'},{'kind':'text','text':'Z'}])
                read=lambda browser:pool.workers[browser].executor.submit(lambda:browser.page.locator('#field').input_value()).result()
                self.assertEqual(read(a),'abZ');self.assertEqual(read(b),'')
                with self.assertRaises(VKError):pool.call(a,'remote_batch',[{'kind':'text','text':'must not apply'},{'kind':'key','key':'Control+L'}])
                self.assertEqual(read(a),'abZ')
                pool.call(a,'close');self.assertTrue(pool.call(b,'remote_screen').startswith(b'\xff\xd8'))
            finally:pool.shutdown()

    def test_browser_dispatches_network_after_login_without_screen_polling(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ',{'VK_BROWSER_HEADLESS':'1'}):
            pool=BrowserPool();browser=BrowserVK(tmp,pool);received=threading.Event()
            def open_page():
                browser._ensure()
                def route(r):
                    if r.request.url.endswith('/after-login'):received.set()
                    r.fulfill(body='<html>fixture</html>',content_type='text/html')
                browser.context.route('https://vk.ru/**',route)
                browser.page.goto('https://vk.ru/')
                browser.page.evaluate("setTimeout(()=>fetch('/after-login'),300)")
                return browser.info()
            browser._open=open_page
            browser._login_progress=lambda:{'state':'connected'}
            try:
                pool.call(browser,'open');pool.call(browser,'login_progress')
                # No screen/status/Playwright calls while the browser fetches.
                self.assertTrue(received.wait(2),'Network dispatch stopped when UI polling stopped')
            finally:pool.shutdown()

    def test_login_navigation_rejects_non_vk_domains(self):
        from unittest.mock import Mock
        browser=BrowserVK('/tmp/unused-vk-test',pool=Mock())
        for url in ('http://127.0.0.1/', 'https://evilvk.com/', 'https://vk.com.evil.test/'):
            route=Mock();route.request.url=url;route.request.is_navigation_request.return_value=True
            browser._login_navigation(route);route.abort.assert_called_once();route.continue_.assert_not_called()
