"""Independent browser worker per operator; no shared browser/resource cap."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time
from vk_api import VKError


class BrowserBusy(VKError):
    """This operator is logging in; no publishing action started."""


@dataclass
class Worker:
    executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix='vk-operator'))
    lease_until: float = 0
    pending: int = 0
    pumping: bool = False


class BrowserPool:
    INTERACTIVE = {'connect', 'open', 'close', 'remote_screen', 'remote_input',
                   'remote_batch', 'finish_login', 'reconnect', 'login_progress',
                   'screen_start', 'screen_close', 'screen_access',
                   'desktop_heartbeat', 'desktop_verify', 'desktop_release'}
    LEASED = INTERACTIVE - {'connect', 'open', 'close', 'screen_close'}

    def __init__(self, idle_seconds=None, login_seconds=600):
        self.lock = threading.RLock()
        self.workers = {}
        self.login_seconds = login_seconds
        self.closed = False

    def available(self, browser):
        with self.lock:
            worker = self.workers.get(browser)
            return not self.closed and (not worker or time.monotonic() >= worker.lease_until)

    def info(self):
        with self.lock:
            return {'limit': None, 'queued': sum(w.pending for w in self.workers.values()),
                    'browser_loaded': any(b.info().get('opened') for b in self.workers),
                    'browser_count': sum(bool(b.info().get('opened')) for b in self.workers),
                    'idle_seconds': None,
                    'login_reserved': any(time.monotonic() < w.lease_until for w in self.workers.values())}

    def call(self, browser, action, *args):
        with self.lock:
            if self.closed:
                raise VKError('Сервер останавливается.')
            worker = self.workers.get(browser)
            if action in self.LEASED and not worker:
                raise BrowserBusy('Открой подключение своего VK.')
            if not worker:
                worker = self.workers[browser] = Worker()
            worker.pending += 1
            # Submit under the lock so shutdown cannot close the executor here.
            future = worker.executor.submit(self._invoke, browser, worker, action, args)
        try:
            return future.result()
        finally:
            with self.lock:
                worker.pending -= 1

    def _invoke(self, browser, worker, action, args):
        try:
            return self._dispatch(browser, worker, action, args)
        finally:
            # Sync Playwright dispatches routes/events only while its event loop
            # runs. Keep that loop alive even when no operator polls the screen.
            with self.lock:
                if not self.closed and not worker.pumping and (getattr(browser, 'context', None) or getattr(browser, 'desktop', None)):
                    worker.pumping = True
                    worker.executor.submit(self._pump, browser, worker)

    def _pump(self, browser, worker):
        with self.lock:
            if self.closed:
                worker.pumping = False
                return
        try:
            browser._pump_events()
        except Exception:
            # Navigation may replace/close a page. Do not reset a login here.
            time.sleep(.05)
        finally:
            with self.lock:
                if not self.closed and (getattr(browser, 'context', None) or getattr(browser, 'desktop', None)):
                    worker.executor.submit(self._pump, browser, worker)
                else:
                    worker.pumping = False

    def _dispatch(self, browser, worker, action, args):
        with self.lock:
            reserved = time.monotonic() < worker.lease_until
            if action in self.LEASED and not reserved:
                raise BrowserBusy('Время входа истекло. Открой подключение VK заново.')
            if action not in self.INTERACTIVE and reserved:
                raise BrowserBusy('Подтверди вход в VK. После подключения задача продолжится.')
        if action == 'close':
            browser._close()
            with self.lock:
                worker.lease_until = 0
            return browser.info()
        result = getattr(browser, '_' + action)(*args)
        with self.lock:
            if action in ('finish_login', 'screen_close') or (action == 'login_progress' and result.get('state') == 'connected'):
                # Keep the authenticated browser warm; only remote input expires.
                worker.lease_until = 0
            elif action in ('login_progress', 'desktop_heartbeat') and getattr(browser, 'desktop', None) and browser.desktop.screen_port:
                worker.lease_until = time.monotonic() + self.login_seconds
            elif action in ('connect', 'open', 'remote_input', 'remote_batch', 'reconnect'):
                worker.lease_until = time.monotonic() + self.login_seconds
        return result

    def collect(self):
        # Intentionally no idle shutdown. The next task reuses the same browser.
        pass

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            workers = list(self.workers.items())
        jobs = [(w, w.executor.submit(b._close)) for b, w in workers]
        try:
            for _, job in jobs:
                job.result()
        finally:
            for worker, _ in jobs:
                worker.executor.shutdown(wait=True)
