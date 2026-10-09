"""Browser-free runtime while VK publication permission is pending.

No OAuth flow is advertised until the publication-capable application is known.
This module never launches a process, reads a browser profile or accepts tokens.
"""
import os
from pathlib import Path
from vk_api import VKError

PENDING = ('Отправка через VK API пока недоступна: нужно зарегистрировать приложение '
           'и получить у VK разрешение wall. Объявления и интервалы сохранены.')

def enabled():
    # Retained only for historical regression tests and explicit local recovery.
    return os.environ.get('VK_POSTER_LEGACY_BROWSER', '1') != '1'

def info():
    return {'transport': 'api', 'state': 'awaiting_vk_approval', 'ready': False,
            'browser_removed': True, 'message': PENDING}

class RetiredBrowser:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.pool = None
    def info(self):
        return {'opened': False, 'authenticated': False, 'message': 'Браузер VK отключён. ' + PENDING}
    def call(self, action, *args):
        if action == 'close':
            return self.info()
        raise VKError(PENDING)

class ApiRuntime:
    """Compatibility lifecycle for the gateway; zero browser workers."""
    def available(self, browser): return True
    def info(self):
        return {'limit': 0, 'queued': 0, 'browser_loaded': False,
                'login_reserved': False, 'transport': 'api'}
    def call(self, browser, action, *args): return browser.call(action, *args)
    def collect(self): pass
    def shutdown(self): pass
