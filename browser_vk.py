"""Dedicated browser session. It never exports cookies or asks for a VK password.

All Playwright operations belong to one worker thread. Unknown layouts fail
closed; the wall and suggestion routes never fall back to each other.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import base64
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse

from vk_api import VKError, UncertainDelivery


class BrowserVK:
    def __init__(self, folder, pool=None):
        self.folder = Path(folder).resolve()
        self.pool = pool
        self.executor = None if pool else ThreadPoolExecutor(max_workers=1, thread_name_prefix='vk-browser')
        self.context = None
        self.playwright = None
        self.page = None
        self.status_lock = threading.Lock()
        self.status = {'opened': False, 'message': 'Открой браузер бота и войди в VK.'}
        self.authenticated = False
        self.screen_session = None
        self.screen_page = None
        self.auth_api_state = None
        self.display = None
        self.desktop = None
        self.health_events = []
        self.health_written_at = 0
        self.health_wait_at = 0

    def _health(self, stage, evidence=None):
        # Diagnostic allowlist only: never URLs, input, IDs or response bodies.
        if os.environ.get('VK_BROWSER_HEALTH') != '1':
            return
        now = time.time()
        if stage == 'waiting':
            if now - self.health_wait_at < 5:
                return
            self.health_wait_at = now
        event = {'at': round(now, 3), 'stage': stage, 'probe': self.auth_api_state}
        if evidence is not None:
            event['evidence'] = {k: bool(evidence.get(k)) for k in ('signedIn','menu','loginForm','loading')}
        previous = self.health_events[-1] if self.health_events else None
        if previous and previous['stage'] == stage and previous['probe'] == self.auth_api_state and stage != 'waiting':
            previous['count'] = previous.get('count', 1) + 1
            previous['last_at'] = round(now, 3)
            if now - self.health_written_at < 1:
                return
        else:
            self.health_events = (self.health_events + [event])[-30:]
        self.health_written_at = now
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'vk-health.json'
        temporary = self.folder / 'vk-health.json.tmp'
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'events': self.health_events}, f)
            os.replace(temporary, path)
        except OSError:
            pass  # Diagnostics must not break browser interaction.

    def info(self):
        with self.status_lock:
            return {**self.status, 'authenticated': self.authenticated}

    def _status(self, opened, message):
        with self.status_lock:
            self.status = {'opened': opened, 'message': message}

    def call(self, action, *args):
        # The caller waits on the same job; a timeout must not start a retry.
        if self.pool:
            return self.pool.call(self, action, *args)
        return self.executor.submit(getattr(self, '_' + action), *args).result()

    def _close(self):
        self.screen_session = None
        self.screen_page = None
        if self.context:
            self.context.close()
            self.context = None
            self.page = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None
        if self.desktop:
            self.desktop.close()
            self.desktop = None
        if self.display:
            self.display.close()
            self.display = None
        self.authenticated = False
        self._health('closed')
        self._status(False, 'Браузер закрыт. Подключи VK, чтобы продолжить.')
        return self.info()

    def _native_window(self, initial_url='https://vk.ru/'):
        if self.desktop and self.desktop.chrome and self.desktop.chrome.poll() is None:
            return self.desktop.endpoint
        from desktop_browser import DesktopBrowser
        from virtual_display import VirtualDisplay
        if self.desktop:
            self.desktop.close()
        if self.display:
            self.display.close()
        profile = self.folder / 'vk-browser'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        self.display = VirtualDisplay()
        self.desktop = DesktopBrowser()
        try:
            return self.desktop.start(profile, self.display.start(), initial_url=initial_url)
        except Exception:
            self.desktop.close()
            self.display.close()
            self.desktop = self.display = None
            raise VKError('Не удалось открыть Chrome. Попробуй подключить экран заново.') from None

    def _detach_control(self):
        # Disconnect automation only. Do not close native Chrome or its tabs.
        self.screen_session = self.screen_page = None
        self.context = self.page = None
        if self.playwright:
            self.playwright.stop()
            self.playwright = None

    def _ensure(self):
        from playwright.sync_api import sync_playwright
        if self.context:
            try:
                if self.page and not self.page.is_closed():
                    return
                self.page = self.context.new_page()
                return
            except Exception:
                self.context = None
        browsers = Path(__file__).resolve().parent / '.browsers'
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(browsers))
        profile = self.folder / 'vk-browser'
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        if not self.playwright:
            self.playwright = sync_playwright().start()
        try:
            if os.environ.get('VK_BROWSER_DESKTOP') == '1':
                endpoint = self._native_window()
                native = self.playwright.chromium.connect_over_cdp(endpoint, no_defaults=True)
                self.context = native.contexts[0]
            else:
                launch_env = None
                if os.environ.get('VK_BROWSER_VIRTUAL_DISPLAY') == '1':
                    from virtual_display import VirtualDisplay
                    if self.display:
                        self.display.close()
                    self.display = VirtualDisplay()
                    launch_env = dict(os.environ, DISPLAY=self.display.start())
                self.context = self.playwright.chromium.launch_persistent_context(
                    str(profile), headless=os.environ.get('VK_BROWSER_HEADLESS') == '1',
                    env=launch_env,
                    viewport={'width': 1360, 'height': 960}, locale='ru-RU',
                    args=([f"--remote-debugging-port={int(os.environ['VK_BROWSER_DEBUG_PORT'])}",
                           '--remote-debugging-address=127.0.0.1'] if os.environ.get('VK_BROWSER_DEBUG_PORT') else []),
                )
            self.context.on('response', self._observe_auth_response)
            self.context.set_default_timeout(12000)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self._health('opened')
            self._status(True, 'Браузер открыт. Войди в VK, затем проверь группу в панели.')
        except Exception:
            if self.desktop:
                self.desktop.close()
                self.desktop = None
            if self.display:
                self.display.close()
                self.display = None
            self._status(False, 'Не удалось запустить браузер. Проверь установку Chromium и графический экран.')
            raise VKError(self.info()['message']) from None

    def _pump_events(self):
        if self.desktop:
            self.desktop.expire()
        pages = [p for p in self.context.pages if not p.is_closed()] if self.context else []
        if pages:
            pages[0].wait_for_timeout(25)
        else:
            time.sleep(.025)

    def _observe_auth_response(self, response):
        # Observe only the status of VK's own account probe. Do not retain its
        # body, viewer details, request parameters, tokens or cookies.
        parsed = urlparse(response.url)
        auth_methods = {'auth.getQrAuthData','auth.getAuthCode','auth.checkAuthCode',
                        'auth.exchangeToken','auth.validateAccount','auth.getSilentToken'}
        method = parsed.path.removeprefix('/method/')
        if parsed.hostname in ('api.vk.ru','api.vk.com','id.vk.ru','id.vk.com') and method in auth_methods:
            try:
                data = response.json()
                code = data.get('error', {}).get('error_code')
                outcome = 'response' if 'response' in data else 'error'
                if type(code) is int and 0 <= code <= 100000:
                    outcome += '_' + str(code)
                # A successful poll response is NOT proof of QR confirmation.
                # Never persist any payload field from the auth response.
                self._health(method + ':' + outcome)
            except Exception:
                self._health(method + ':unreadable')
            return
        if parsed.hostname not in ('web.api.vk.ru', 'web.api.vk.com') or parsed.path != '/method/account.getInfo':
            return
        try:
            data = response.json()
            if data.get('error', {}).get('error_code') == 5:
                self.auth_api_state = 'rejected'
                self.authenticated = False
                self._health('vk_rejected')
            elif 'response' in data:
                self.auth_api_state = 'accepted'
                self._health('vk_accepted')
        except Exception:
            pass

    def _connect(self):
        self.login_confirmed_at = None
        self.login_started = time.monotonic()
        if os.environ.get('VK_BROWSER_DESKTOP') == '1':
            self._detach_control()
            self._native_window()
            self.authenticated = False
            self.auth_api_state = None
            self._health('manual_login')
            self._status(True, 'Войди в VK. После загрузки аккаунта нажми «Я вошёл — проверить».')
            return self.info()
        return self._open()

    def _desktop_heartbeat(self):
        if not self.desktop or not self.desktop.screen_port:
            raise VKError('Экран закрыт. Подключи его заново.')
        self.desktop.screen_until = time.monotonic() + 30
        return {'state': 'manual', 'message': 'Войди в VK. После загрузки аккаунта нажми «Я вошёл — проверить».'}

    def _desktop_verify(self):
        if self.desktop:
            self.desktop.screen_until = time.monotonic() + 75
        self._ensure()
        self.login_confirmed_at = None
        self.login_started = time.monotonic()
        self.auth_api_state = None
        self._health('manual_verification_started')
        page = self._login_page()
        evidence = self._auth_evidence(page)
        if evidence.get('challenge'):
            self._detach_control()
            return {'state': 'manual', 'message': 'VK просит подтвердить «Я не робот». Пройди проверку в окне, затем нажми «Я вошёл — проверить».'}
        if evidence.get('loginForm'):
            self._detach_control()
            return {'state': 'manual', 'message': 'VK ещё показывает форму входа. Заверши вход в окне.'}
        # One explicit reload checks the saved login with fresh natural VK requests.
        # Never reload from background polling and never synthesize VK API calls.
        page.reload(wait_until='domcontentloaded', timeout=45000)
        return self._login_progress()

    def _desktop_release(self):
        self._detach_control()
        self._health('manual_login')
        return {'state': 'manual'}

    def _login_progress(self):
        if self.desktop and not self.context:
            return self._desktop_heartbeat()
        if self.desktop and self.desktop.screen_port:
            self.desktop.screen_until = time.monotonic() + 30
        page = self._login_page()
        try:
            self._confirm_login(page, timeout=0)
            now = time.monotonic()
            if getattr(self, 'login_confirmed_at', None) is None:
                self.login_confirmed_at = now
            if now - self.login_confirmed_at < 3:
                self.authenticated = False
                return {'state': 'waiting', 'message': 'VK ответил. Проверяю устойчивость подключения…'}
            self.page = page
            self._health('connected')
            self._screen_close()
            return {'state': 'connected', 'message': 'VK подключён.'}
        except VKError:
            self.login_confirmed_at = None
            try:
                evidence = self._auth_evidence(page)
                self._health('waiting', evidence)
            except Exception:
                return {'state': 'waiting', 'message': 'VK выполняет переход. Подключаю…'}
            if evidence.get('challenge'):
                return {'state': 'manual', 'message': 'VK просит подтвердить «Я не робот». Пройди проверку в окне.'}
            elapsed = time.monotonic() - getattr(self, 'login_started', time.monotonic())
            # An anonymous/stale account probe may return error 5 while VK is
            # still rendering login. Never erase a session from a status poll.
            if self.auth_api_state == 'rejected' and not evidence.get('loginForm') and elapsed > 10:
                return {'state': 'session_rejected', 'message':
                        'VK отклонил сессию (invalid session). Вход не подтверждён. Это не просто загрузка. Можно перезагрузить VK; автоматическая очистка входа отключена.'}
            if evidence.get('loginForm'):
                message = 'Подтверди вход на странице VK. Дальше бот всё завершит сам.'
            elif elapsed > 45:
                return {'state': 'stalled', 'message':
                        'VK пока не завершил загрузку. Если ниже только индикатор, нажми «Перезагрузить VK». Сессия не очищается.'}
            else:
                message = 'Загружается форма VK… Дождись поля входа или QR-кода.'
            return {'state': 'waiting', 'message': message}

    def _reconnect(self):
        # Explicit operator action, under their own login lease. Other
        # operators' contexts and the application account stay untouched.
        self._ensure()
        # This method is called only by the explicit reconnect control, never
        # by polling. Stop old pages before clearing this operator's VK data.
        for page in self.context.pages:
            if not page.is_closed():
                page.goto('about:blank', wait_until='commit')
        session = self.context.new_cdp_session(self.page)
        try:
            for host in ('vk.ru','vk.com','www.vk.ru','www.vk.com',
                         'id.vk.ru','id.vk.com','login.vk.ru','login.vk.com'):
                session.send('Storage.clearDataForOrigin', {
                    'origin': 'https://' + host,
                    'storageTypes': 'local_storage,indexeddb,cache_storage,service_workers'})
        finally:
            session.detach()
        self.context.clear_cookies()
        self.authenticated = False
        self.auth_api_state = None
        self._health('explicit_reconnect')
        if self.desktop:
            self.page.goto('https://vk.ru/', wait_until='commit', timeout=45000)
        return self._connect()

    @staticmethod
    def _vk_login_host(url):
        host = urlparse(url).hostname or ''
        return host in ('vk.ru', 'vk.com') or host.endswith(('.vk.ru', '.vk.com'))

    def _login_navigation(self, route):
        if route.request.is_navigation_request() and not self._vk_login_host(route.request.url):
            route.abort()
        else:
            route.continue_()

    def _login_page(self):
        pages = [page for page in self.context.pages if not page.is_closed()] if self.context else []
        if not pages:
            raise VKError('Открой окно VK заново.')
        page = pages[-1]
        if not self._vk_login_host(page.url):
            raise VKError('В этом окне разрешён только вход на страницах VK.')
        return page

    def _remote_screen(self):
        page = self._login_page()
        # Capture the already rendered surface, without waiting for all web
        # fonts or page animations. Never store screens on disk.
        if self.screen_page is not page or self.screen_session is None:
            self.screen_session = self.context.new_cdp_session(page)
            self.screen_page = page
        frame = self.screen_session.send('Page.captureScreenshot', {
            'format': 'jpeg', 'quality': 65, 'fromSurface': True,
            'captureBeyondViewport': False})
        return base64.b64decode(frame['data'])

    def _remote_batch(self, events):
        # Bounded payload, not a resource/concurrency cap. Validate the entire
        # packet before applying input; never log text/passwords.
        if not isinstance(events, list) or not 1 <= len(events) <= 64:
            raise VKError('Некорректный пакет ввода.')
        allowed_keys = {'Enter','Tab','Shift+Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown','Control+A'}
        size = self._login_page().viewport_size or {'width':1360, 'height':960}
        for event in events:
            if not isinstance(event, dict):
                raise VKError('Некорректный ввод.')
            kind = event.get('kind')
            valid = False
            if kind == 'text':
                valid = isinstance(event.get('text'), str) and len(event['text']) <= 2000
            elif kind == 'key':
                valid = event.get('key') in allowed_keys
            elif kind == 'click':
                valid = all(type(event.get(k)) in (int, float) and 0 <= event[k] < size[dimension] for k, dimension in [('x','width'),('y','height')])
            elif kind == 'scroll':
                valid = type(event.get('delta')) in (int, float) and -2000 <= event['delta'] <= 2000
            if not valid:
                raise VKError('Некорректный ввод.')
        for event in events:
            self._remote_input(event)
        return {'ok': True}

    def _remote_input(self, event):
        page = self._login_page()
        kind = event.get('kind')
        if kind == 'click':
            x, y = event.get('x'), event.get('y')
            size = page.viewport_size or {'width':1360, 'height':960}
            if type(x) not in (int,float) or type(y) not in (int,float) or not (0 <= x < size['width'] and 0 <= y < size['height']):
                raise VKError('Некорректная позиция клика.')
            page.mouse.click(x, y)
        elif kind == 'text':
            text = event.get('text')
            if not isinstance(text, str) or len(text) > 2000:
                raise VKError('Слишком длинный ввод.')
            page.keyboard.insert_text(text)
        elif kind == 'key':
            key = event.get('key')
            if key not in ('Enter','Tab','Shift+Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown','Control+A'):
                raise VKError('Эта клавиша не поддерживается.')
            page.keyboard.press(key)
        elif kind == 'scroll':
            delta = event.get('delta')
            if type(delta) not in (int,float) or not -2000 <= delta <= 2000:
                raise VKError('Некорректная прокрутка.')
            page.mouse.wheel(0, delta)
        else:
            raise VKError('Неизвестное действие.')
        return {'ok': True}

    def _open(self):
        self._ensure()
        self.context.unroute('**/*', self._login_navigation)
        if not self.desktop:
            self.context.route('**/*', self._login_navigation)
        self.auth_api_state = None
        try:
            self.page.goto('https://vk.ru/', wait_until='domcontentloaded', timeout=45000)
            self.page.bring_to_front()
        except Exception:
            raise VKError('Браузер открыт, но VK не загрузился. Проверь соединение в его окне.') from None
        return self.info()

    def _screen_start(self):
        if not self.desktop:
            raise VKError('Встроенный рабочий стол не включён.')
        self.desktop.open_screen()
        return {'ready': True}

    def _screen_close(self):
        if self.desktop:
            self.desktop.close_screen()
        return {'ok': True}

    def _screen_access(self):
        if not self.desktop or not self.desktop.screen_port:
            raise VKError('Экран закрыт. Открой подключение VK заново.')
        self.desktop.expire()
        if not self.desktop.screen_port:
            raise VKError('Время подключения экрана истекло.')
        return self.desktop.screen_port

    @staticmethod
    def unique(locator, label):
        visible = [item for item in locator.all() if item.is_visible()]
        if len(visible) != 1:
            raise VKError(f'Не удалось однозначно найти {label}. Нужна настройка под текущий интерфейс VK.')
        return visible[0]

    def _navigate(self, group):
        self._ensure()
        url = group['url']
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('vk.com', 'vk.ru'):
            raise VKError('Неверный адрес группы.')
        self.auth_api_state = None
        self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        self._confirm_login(self.page)
        # Authentication and group rendering are separate states. A stalled
        # React shell must never be reported as a lost account session.
        try:
            self.page.locator('[data-testid="group-name"], .page_name').first.wait_for(timeout=30000)
        except Exception:
            raise VKError('Вход в VK подтверждён, но интерфейс группы не загрузился. '
                          'Открой окно VK и проверь загрузку страницы. Объявление не отправлено.') from None
        self.page.wait_for_timeout(1500)

    @staticmethod
    def _auth_evidence(page):
        # Read only a boolean from VK's bootstrapped viewer state, never the
        # user ID, cookies or tokens. VK exposes it before rendering the menu.
        return page.evaluate("""() => {
            const visible = s => [...document.querySelectorAll(s)].some(e => e.getClientRects().length);
            const viewer = Number(window.vk?.id);
            return {
                signedIn: (Number.isSafeInteger(viewer) && viewer > 0) ||
                    visible('#top_profile_link, [data-testid="header-profile-menu-button"]'),
                menu: visible('#top_profile_link, [data-testid="header-profile-menu-button"]'),
                loginForm: visible('input[type="password"], input[name="login"], input[type="tel"]'),
                loading: visible('[data-testid="loading-skeleton"]'),
                challenge: visible('iframe[src*="captcha"]') || /Подтвердите,?\\s*что вы не робот|Я не робот/i.test(document.body.innerText)
            };
        }""")

    def _confirm_login(self, page, timeout=15000):
        deadline = time.monotonic() + timeout / 1000
        evidence = {}
        while True:
            if self._vk_login_host(page.url):
                try:
                    evidence = self._auth_evidence(page)
                    if self.auth_api_state == 'rejected':
                        break
                    if evidence['signedIn'] and not evidence.get('challenge') and not evidence['loading'] and not evidence['loginForm'] and (self.auth_api_state == 'accepted' or
                            (evidence['menu'] and not evidence['loading'])):
                        self.authenticated = True
                        self._status(True, 'Вход в VK подтверждён. Доступность публикации проверяется отдельно для каждой группы.')
                        return
                except Exception:
                    # OAuth can replace the execution context while loading.
                    pass
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(250)
        self.authenticated = False
        host = urlparse(page.url).hostname or ''
        if self.auth_api_state == 'rejected':
            message = ('VK узнаёт аккаунт, но отклоняет его сессию (ошибка авторизации 5). '
                       'Открой «Подключить VK» и заверши вход. Сессия автоматически не очищается. Объявление не отправлено.')
        elif evidence.get('loginForm') or host.startswith(('id.', 'login.')):
            message = 'VK ещё показывает страницу входа. Заверши авторизацию в окне бота; окно осталось открытым.'
        elif evidence.get('loading'):
            message = 'VK завис на загрузке интерфейса. Вход пока не удалось проверить; окно осталось открытым.'
        else:
            message = 'VK не подтвердил вход: страница не загрузилась или требует дополнительного действия. Проверь открытое окно VK.'
        self._status(True, message)
        raise VKError(message)

    def _finish_login(self):
        page = self._login_page()
        self._confirm_login(page)
        self.page = page
        return self.info()

    def _composer(self, group):
        self._navigate(group)
        # Observed current desktop UI in the supplied test community. Its
        # management composer posts as the community; an author signature is
        # not a personal-profile post. Do not silently change authorship.
        if group['mode'] == 'browser_wall':
            try:
                self.page.get_by_test_id('group_publish_create_button').wait_for(timeout=4000)
            except Exception:
                pass
            if self.page.get_by_test_id('group_publish_create_button').count():
                raise VKError('Вход подтверждён. Найден новый редактор сообщества. Публикация от личного профиля здесь не подтверждена; отправка заблокирована, чтобы не сменить автора на сообщество.')
        if group['mode'] == 'browser_suggest':
            # Current VK uses «Предложить пост». Prefer semantic controls,
            # keeping the old wording for communities on the legacy layout.
            entry = self.page.locator('[data-testid="group_publish_suggest_button"]').or_(
                self.page.get_by_role('button', name=re.compile(r'^Предложить (новость|пост)$'))
            ).or_(self.page.get_by_role('link', name=re.compile(r'^Предложить (новость|пост)$')))
            try:
                entry.filter(visible=True).first.wait_for(timeout=15000)
            except Exception:
                raise VKError('В этой группе не найдена доступная кнопка «Предложить пост» / «Предложить новость». Проверь возможность предложения в окне браузера бота; запись не отправлена.') from None
            self.unique(entry, 'кнопку предложения поста').click()
            modern = self.page.get_by_test_id('posting_modal_box').get_by_test_id('posting_base_screen_input_message')
            self.page.locator('#post_field:visible').or_(modern).first.wait_for(timeout=15000)
            if modern.is_visible():
                modal = self.page.get_by_test_id('posting_modal_box')
                next_button = self.unique(modal.get_by_test_id('posting_base_screen_next'), 'кнопку «Далее»')
                return modern, next_button
        elif group['mode'] != 'browser_wall':
            raise VKError('Неизвестный браузерный режим.')
        field = self.unique(self.page.locator('#post_field'), 'редактор записи')
        field.click()
        button = self.unique(self.page.locator('#send_post'), 'кнопку отправки записи')
        label = button.inner_text().strip()
        allowed = ('Предложить новость', 'Предложить') if group['mode'] == 'browser_suggest' else ('Опубликовать', 'Отправить')
        if label not in allowed:
            raise VKError('Форма VK не соответствует выбранному способу отправки. Отправка остановлена.')
        # For the supported desktop composer, an enabled community-author
        # switch would violate the requirement to post as the user.
        author_switch = self.page.locator('#post_from_group')
        if author_switch.count():
            active = author_switch.evaluate("el => el.checked === true || el.getAttribute('aria-checked') === 'true' || el.classList.contains('on')")
            if active:
                raise VKError('В редакторе выбрана публикация от сообщества. Переключи на личный профиль.')
        return field, button

    def _prepare_modern_suggestion(self, field, next_button, draft, photo_folder):
        """Prepare the observed two-step suggestion editor; never submit here."""
        modal = self.page.get_by_test_id('posting_modal_box')
        if (field.inner_text().strip() or modal.get_by_test_id('posting_attachment_item').count()
                or modal.get_by_test_id('media-grid-item').count()):
            raise VKError('В редакторе уже есть текст или вложения. Сохрани или удали старый черновик перед автопостингом.')
        field.fill(draft['text'])
        if draft['photos']:
            modal.get_by_test_id('posting_base_screen_download_from_device').set_input_files(
                [str(Path(photo_folder) / name) for name in draft['photos']])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                # posting_attachment_item includes a wrapper: counting it
                # overcounts. The observed carousel has one remove control
                # and one loaded image per actual photo, including offscreen.
                grid = modal.get_by_test_id('media-grid')
                photos = grid if grid.count() else modal.get_by_test_id('sortable-carousel-track')
                loaded = photos.locator('img').evaluate_all(
                    'es => es.filter(e => e.complete && e.naturalWidth > 0).length')
                count = (grid.get_by_test_id('media-grid-item').count() if grid.count()
                         else photos.get_by_test_id('posting_attachment_photo_item_remove').count())
                if (count == len(draft['photos'])
                        and loaded == len(draft['photos'])
                        and not modal.locator('[role="progressbar"]:visible').count()
                        and next_button.is_enabled()):
                    break
                self.page.wait_for_timeout(500)
            else:
                raise VKError('Не подтверждена загрузка всех фото. Запись не отправлена.')
            if len(draft['photos']) > 1:
                self._select_photo_grid(modal, len(draft['photos']))
        if field.inner_text().replace('\r\n', '\n').strip() != draft['text'].strip():
            raise VKError('Текст в редакторе отличается от объявления. Запись не отправлена.')
        next_button.click()
        submit = modal.get_by_test_id('posting_suggest_button')
        try:
            submit.wait_for(timeout=15000)
        except Exception:
            raise VKError('Не найден финальный экран предложения поста. Запись не отправлена.') from None
        button = self.unique(submit, 'кнопку отправки предложения')
        if button.inner_text().strip() not in ('Предложить пост', 'Предложить новость'):
            raise VKError('Форма VK не соответствует предложению новости. Запись не отправлена.')
        preview = modal.get_by_test_id('showmoretext-in')
        normalize = lambda text: ' '.join(text.split())
        if normalize(preview.inner_text()) != normalize(draft['text']):
            raise VKError('Текст в предпросмотре отличается от объявления. Запись не отправлена.')
        grid_count = modal.get_by_test_id('media-grid-item').count()
        preview_count = grid_count or modal.get_by_test_id('posting_preview_attachment_item').count()
        if preview_count != len(draft['photos']):
            raise VKError('В предпросмотре не подтверждены все фото. Запись не отправлена.')
        if len(draft['photos']) > 1 and grid_count != len(draft['photos']):
            raise VKError('В предпросмотре не подтверждена раскладка фото сеткой. Запись не отправлена.')
        # This is a personal submission to moderation. Keep attribution enabled;
        # the community's administrator still controls eventual publication.
        signature = modal.get_by_test_id('posting_settings_sign_switch_input')
        if not signature.is_checked():
            modal.get_by_test_id('posting_settings_sign_switch').click()
        if not signature.is_checked():
            raise VKError('Не удалось включить подпись автора. Запись не отправлена.')
        story = modal.get_by_test_id('posting_settings_repost_to_story_switch')
        if story.count():
            story_input = story if story.get_attribute('type') == 'checkbox' else story.locator('input[type="checkbox"]')
            if story_input.is_checked():
                story.click()
            if story_input.is_checked():
                raise VKError('Не удалось отключить публикацию в истории. Запись не отправлена.')
        return button

    def _select_photo_grid(self, modal, count):
        """VK defaults to a carousel; explicitly choose its square-grid layout."""
        if not modal.get_by_role('button', name='Сетка', exact=True).is_visible():
            self.unique(modal.get_by_role('button', name='Карусель', exact=True),
                        'переключатель раскладки фото').click()
            option = self.page.get_by_test_id('posting_base_screen_view_option').filter(
                has_text=re.compile(r'^Сетка\s*$'))
            option.wait_for(timeout=10000)
            self.unique(option, 'раскладку «Сетка»').click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            items = modal.get_by_test_id('media-grid-item')
            if (items.count() == count and
                    items.evaluate_all('es=>es.every(e=>Array.from(e.querySelectorAll("img")).some(i=>i.complete && i.naturalWidth>0))') and
                    modal.get_by_role('button', name='Сетка', exact=True).is_visible()):
                return
            self.page.wait_for_timeout(200)
        raise VKError('Не удалось подтвердить все фото в раскладке «Сетка». Запись не отправлена.')

    def _inspect(self, group, draft=None, photo_folder=None):
        owned_draft = False
        try:
            field, button = self._composer(group)
            if not draft or not (draft['text'].strip() or draft['photos']):
                raise VKError('Редактор найден. Сначала сохрани объявление с текстом или фото для проверки подготовки.')
            if field.get_attribute('data-testid') != 'posting_base_screen_input_message':
                raise VKError('Полная проверка доступна для нового редактора «Предложить пост».')
            modal = self.page.get_by_test_id('posting_modal_box')
            if (field.inner_text().strip() or modal.get_by_test_id('posting_attachment_item').count()
                    or modal.get_by_test_id('media-grid-item').count()):
                raise VKError('В редакторе есть чужой черновик. Сохрани или убери его перед проверкой.')
            owned_draft = True
            submit = self._prepare_modern_suggestion(field, button, draft, photo_folder)
            if not submit.is_enabled():
                raise VKError('VK не разрешает предложить подготовленную запись.')
            self._status(True, 'Проверены текст, фото сеткой и готовность предложения. Тестовый черновик удалён; запись не отправлялась.')
            return {'composer_found': True, 'message': self.info()['message']}
        except VKError:
            raise
        except Exception:
            raise VKError('Не удалось проверить редактор VK. Проверь страницу в окне браузера бота.') from None
        finally:
            if owned_draft:
                try:
                    self.page.get_by_test_id('modal-close-button').click()
                    discard = self.page.get_by_text('Выйти без сохранения', exact=True)
                    discard.wait_for(timeout=3000)
                    discard.click()
                    self.page.get_by_test_id('posting_modal_box').wait_for(state='hidden', timeout=5000)
                except Exception:
                    raise VKError('Проверка остановлена: не удалось закрыть тестовый черновик. Закрой его без сохранения в браузере бота.') from None

    def _publication(self, group, watch):
        from publication_vk import scan_wall
        self._ensure()
        original = self.page
        check_page = self.context.new_page()
        try:
            self.page = check_page
            self._navigate(group)
            try:
                result = scan_wall(check_page, watch)
            except VKError:
                receipt = self._read_suggested(check_page, watch)
                if not receipt:
                    raise
                return {'state': 'pending', 'url': '', 'suggested': True,
                        'suggestion_url': receipt['url'],
                        'detail': 'Новость найдена в предложенных. VK принял её; публикация ожидает решения администратора.'}
            if result['state'] != 'found':
                result['suggested'] = False
                receipt = self._read_suggested(check_page, watch)
                if receipt:
                    result.update(suggested=True, suggestion_url=receipt['url'],
                                  detail='Новость найдена в предложенных. VK принял её; публикация ожидает решения администратора.')
            return result
        finally:
            check_page.close()
            self.page = original

    def _read_suggested(self, page, watch):
        """Read VK's explicit suggested-posts section; never approve a post."""
        from publication_vk import scan_wall
        link = page.get_by_test_id('group_unpublished_button')
        page.evaluate('window.scrollTo(0, 0)')
        try:
            link.wait_for(timeout=5000)
        except Exception:
            return None
        href = link.get_attribute('href') or ''
        if not re.fullmatch(r'/wall-\d+\?suggested=1', href):
            return None
        page.goto('https://vk.ru' + href, wait_until='domcontentloaded', timeout=45000)
        result = scan_wall(page, watch, suggested=True)
        if result['state'] == 'found':
            return {'url': 'https://vk.ru' + href, 'post_url': result['url']}
        return None

    def _confirm_suggestion(self, group, draft, since):
        self._ensure()
        original = self.page
        check_page = self.context.new_page()
        try:
            self.page = check_page
            self._navigate(group)
            return self._read_suggested(check_page, {'group_url': group['url'],
                'text': draft['text'], 'photo_count': len(draft['photos']), 'since': since})
        finally:
            check_page.close()
            self.page = original

    def _post(self, group, draft, photo_folder, before_submit=None):
        try:
            field, button = self._composer(group)
            if before_submit is not None:
                from publication_vk import POSTS
                known = self.page.locator(POSTS).evaluate_all(
                    "es=>es.map(e=>e.getAttribute('data-post-id')||(e.id||'').replace(/^post/,''))")
                before_submit([pid for pid in known if re.fullmatch(r'-\d+_\d+', pid)])
            if field.get_attribute('data-testid') == 'posting_base_screen_input_message':
                if group['mode'] != 'browser_suggest':
                    raise VKError('Новый редактор поддерживается только для предложения поста.')
                button = self._prepare_modern_suggestion(field, button, draft, photo_folder)
            else:
                button = self._prepare_legacy(field, button, draft, photo_folder)
            return self._submit(group, draft, button)
        except (VKError, UncertainDelivery):
            raise
        except Exception:
            raise VKError('Интерфейс VK не распознан или недоступен. Запись не отправлена.') from None

    def _prepare_legacy(self, field, button, draft, photo_folder):
        if field.inner_text().strip():
            raise VKError('В редакторе VK уже есть текст. Удали или сохрани его вручную перед автопостингом.')
        if self.page.locator('#page_add_media .page_preview_photo').count():
            raise VKError('В редакторе уже есть вложения. Очисти старый черновик перед автопостингом.')
        field.fill(draft['text'])
        if draft['photos']:
            # Recognised wall attachment container; no page-wide file inputs.
            media = self.unique(self.page.locator('#page_add_media'), 'блок вложений записи')
            uploader = media.locator('input[type=file][accept*="image"]')
            if uploader.count() != 1:
                raise VKError('Загрузчик фото VK не распознан. Запись не отправлена.')
            uploader.set_input_files([str(Path(photo_folder) / name) for name in draft['photos']])
            thumbs = self.page.locator('#page_add_media .page_preview_photo')
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if thumbs.count() == len(draft['photos']) and not self.page.locator('#page_add_media .progress:visible').count():
                    break
                self.page.wait_for_timeout(500)
            else:
                raise VKError('Не подтверждена загрузка всех фото. Запись не отправлена.')
        if field.inner_text().replace('\r\n', '\n').strip() != draft['text'].strip():
            raise VKError('Текст в редакторе отличается от объявления. Запись не отправлена.')
        return button

    def _submit(self, group, draft, button):
        attempted = False
        submitted_at = time.time()
        try:
            if not button.is_enabled():
                raise VKError('VK не разрешил отправку записи.')
            previous = set(self.page.locator('[id^="post-"]').evaluate_all('items => items.map(x => x.id)'))
            attempted = True
            button.click()
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if group['mode'] == 'browser_suggest':
                    confirmation = self.page.get_by_text(re.compile(r'^(?:Новость (?:предложена|отправлена на (?:рассмотрение|модерацию))|Пост (?:предложен|отправлен на (?:рассмотрение|модерацию)))[.!]?$'))
                    if any(x.is_visible() for x in confirmation.all()):
                        return {'status': 'browser_suggested', 'url': '', 'detail': 'VK подтвердил отправку новости на рассмотрение. Публикация зависит от администратора.'}
                else:
                    posts = self.page.locator('[id^="post-"]')
                    for item in posts.all():
                        pid = item.get_attribute('id') or ''
                        if pid not in previous and re.fullmatch(r'post-\d+_\d+', pid) and draft['text'].strip() and draft['text'].strip() in item.inner_text():
                            return {'status': 'browser_posted', 'url': 'https://vk.ru/wall' + pid[4:], 'detail': 'Новая запись найдена на стене после отправки через браузер.'}
                self.page.wait_for_timeout(500)
            if group['mode'] == 'browser_suggest':
                try:
                    receipt = self._confirm_suggestion(group, draft, submitted_at)
                    if receipt:
                        return {'status': 'browser_suggested', 'url': '',
                                'detail': 'Новость найдена в предложенных. VK принял её; ожидается модерация.'}
                except Exception:
                    pass  # A failed read does not prove the submission failed.
            raise UncertainDelivery('Кнопка отправки нажата, но результат не подтверждён. Проверь VK; автоматического повтора не будет.')
        except (VKError, UncertainDelivery):
            raise
        except Exception:
            if attempted:
                raise UncertainDelivery('После нажатия отправки связь с браузером потеряна. Проверь VK перед повтором.') from None
            raise VKError('Интерфейс VK не распознан или недоступен. Запись не отправлена.') from None
