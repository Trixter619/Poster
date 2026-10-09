import copy
from process_lock import ProcessLock
from launcher import instance_id
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse

from flask import Flask, Response, abort, redirect, jsonify, render_template, request, send_from_directory, send_file
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

from vk_api import VK, VKError, UncertainDelivery, SUGGEST_DENIED
import api_only
from browser_pool import BrowserBusy

ROOT = Path(__file__).resolve().parent
PUBLICATION_INTERVAL = 15 * 60


def watching(job):
    return job.get('publication', {}).get('state') in ('pending', 'error')


def mark_published(job):
    publication = job.get('publication', {})
    if publication.get('state') != 'found':
        return
    # Keep the original transport outcome for diagnostics, while showing the
    # later publication result as the current status.
    job.setdefault('delivery_status', job['status'])
    job.setdefault('delivery_detail', job.get('detail', ''))
    job.update(status='published', detail='Публикация найдена на стене сообщества.',
               url=publication['url'])


def publication_watch(group, draft, since, source='submitted'):
    return {'state': 'pending', 'group_url': group['url'], 'text': draft['text'],
            'photo_count': len(draft['photos']), 'photo_layout': draft.get('photo_layout','grid'), 'since': since, 'source': source,
            'next_check': time.time() + PUBLICATION_INTERVAL, 'checked_at': None,
            'detail': 'Ожидается проверка стены.', 'url': ''}


def group_slug(value):
    if not isinstance(value, str):
        raise ValueError('Укажи ссылку на сообщество VK.')
    value = value.strip()
    if re.fullmatch(r'[1-9]\d*', value):
        return 'club' + value
    if '://' not in value:
        value = 'https://' + value if '/' in value else 'https://vk.com/' + value
    u = urlparse(value)
    if u.scheme not in ('https', 'http') or u.hostname not in ('vk.com', 'vk.ru', 'www.vk.com', 'www.vk.ru', 'm.vk.com', 'm.vk.ru'):
        raise ValueError('Нужна ссылка на группу на vk.com или vk.ru.')
    slug = u.path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.]{2,100}', slug) or slug.startswith(('wall', 'id')):
        raise ValueError('Укажи страницу сообщества, а не ссылку на пост или профиль.')
    return slug


class Store:
    def __init__(self, folder, browser_pool=None, official=False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.folder / 'photos').mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.file = self.folder / 'state.json'
        self.state = json.loads(self.file.read_text()) if self.file.exists() else {
            'draft': {'text': '', 'photos': []}, 'groups': [], 'history': [], 'revision': 0,
        }
        self.state['draft'].setdefault('photo_layout','grid')
        self.state.setdefault('draft_revision', 0)
        self.state.setdefault('tasks', [])
        self.state.setdefault('automation_paused', False)
        for group in self.state['groups']:
            group.setdefault('mode', 'api')
        self.token = os.environ.get('VK_ACCESS_TOKEN', '')
        self.account = None
        self.operator_active = lambda: True
        self.connection_status = lambda: None
        self.busy = set()
        self.official = official
        self.api_only = api_only.enabled() and not official
        if official:
            from official_vk import OfficialVK
            self.browser = OfficialVK(self.folder)
        elif self.api_only:
            self.token = ''
            self.browser = api_only.RetiredBrowser(self.folder)
        else:
            from browser_vk import BrowserVK
            self.browser = BrowserVK(self.folder, pool=browser_pool)

    def save(self):
        self.state['revision'] += 1
        temp = self.file.with_suffix('.tmp')
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, self.file)

    def recover(self):
        with self.lock:
            if self.official:
                self.state['tasks'] = []
                migrated = False
                for g in self.state['groups']:
                    if g['mode'] != 'api_suggest':
                        migrated = True
                        g['mode'] = 'api_suggest'
                    for key in ('browser_checked', 'browser_error', 'previous_transport'):
                        g.pop(key, None)
                if migrated:
                    self.state['automation_paused'] = True
            if self.api_only:
                self.state['automation_paused'] = True
                for g in self.state['groups']:
                    if g['mode'] != 'api':
                        g.setdefault('previous_transport', g['mode'])
                        g['mode'] = 'api'
            if not self.api_only and not self.official:
                for g in self.state['groups']:
                    if g['mode'] == 'api' and g.get('previous_transport') == 'browser_suggest':
                        g['mode'] = 'browser_suggest'
                        g.pop('browser_checked', None)
            for g in self.state['groups']:
                if not self.api_only and not self.official and g['mode'] != 'browser_suggest':
                    g['enabled'] = False
            for h in self.state['history']:
                mark_published(h)
                if h['status'] == 'sending':
                    h.update(status='unknown', detail='Приложение остановилось во время отправки. Проверь VK перед повтором.')
                    if h.get('publication'):
                        h['publication']['state'] = 'pending'
                    for g in self.state['groups']:
                        if g['id'] == h['group_id']:
                            g['enabled'] = False
            self.save()

    def group(self, gid):
        return next((g for g in self.state['groups'] if g['id'] == gid), None)

    def automation_info(self):
        if self.api_only:
            return {'running': False, 'paused': True, 'schedules': 0,
                    'publication_checks': 0, 'busy': bool(self.busy),
                    'blocked_reason': api_only.PENDING,
                    'configured_schedules': sum(bool(g['enabled']) for g in self.state['groups'])}
        schedules = sum(g['enabled'] and ((self.official and g['mode'] == 'api_suggest' and g.get('api_checked')) or (g['mode'] == 'browser_suggest' and g.get('browser_checked') == g['mode'])) for g in self.state['groups'])
        checks = sum(watching(h) for h in self.state['history'])
        return {'running': not self.state['automation_paused'] and bool(schedules or checks),
                'paused': self.state['automation_paused'], 'schedules': schedules,
                'publication_checks': checks, 'busy': bool(self.busy)}

    def add_history(self, job):
        # Pending moderation must survive the ordinary 200-entry history limit.
        old = self.state['history']
        self.state['history'] = [job] + [h for i, h in enumerate(old)
                                        if i < 199 or watching(h) or h['status'] == 'sending']

    def pending_publication(self, gid):
        return any(h['group_id'] == gid and watching(h) for h in self.state['history'])

    def claim_publication(self, jid, automatic=False):
        if self.api_only:
            raise ValueError(api_only.PENDING)
        with self.lock:
            if not self.operator_active():
                raise ValueError('Учётная запись оператора отключена.')
            h = next((h for h in self.state['history'] if h['id'] == jid), None)
            if not h or not watching(h):
                raise ValueError('Для этой отправки нет активной проверки публикации.')
            if self.busy:
                raise ValueError('Браузер занят. Дождись окончания текущей операции.')
            watch = h['publication']
            if automatic and (not self.operator_active() or self.state['automation_paused'] or watch['next_check'] > time.time()):
                return None
            watch['next_check'] = time.time() + PUBLICATION_INTERVAL
            self.busy.add(h['group_id'])
            self.save()
            return (h['id'], h['group_id'], copy.deepcopy(watch))

    def check_publication(self, task):
        jid, gid, watch = task
        try:
            result = self.browser.call('publication', {'url': watch['group_url']}, watch)
            if result.get('state') not in ('pending', 'found'):
                raise VKError('Не удалось распознать результат проверки стены.')
            if result['state'] == 'found' and not re.fullmatch(r'https://vk\.ru/wall-\d+_\d+', result.get('url', '')):
                raise VKError('Не получена корректная ссылка на публикацию.')
        except VKError as error:
            result = {'state': 'error', 'url': '', 'detail': str(error) + ' Повтор через 15 минут.'}
        except Exception:
            result = {'state': 'error', 'url': '',
                      'detail': 'Не удалось проверить стену. Проверь подключение VK и доступ к группе. Повтор через 15 минут.'}
        with self.lock:
            h = next((h for h in self.state['history'] if h['id'] == jid), None)
            if h and watching(h):
                h['publication'].update(result, checked_at=time.time(), next_check=time.time() + PUBLICATION_INTERVAL)
                if result.get('suggested') and h['status'] == 'unknown':
                    h.setdefault('delivery_status', h['status'])
                    h.setdefault('delivery_detail', h.get('detail', ''))
                    h.update(status='browser_suggested', detail='VK принял новость. Она находится в предложенных и ожидает модерации.')
                mark_published(h)
                g = self.group(gid)
                if g and (result.get('suggested') or result['state'] == 'found') and g.get('delivery_pause') == jid:
                    g['enabled'] = bool(h.get('schedule_was_enabled'))
                    g.pop('delivery_pause', None)
                if result['state'] == 'found' and g:
                    # Respect pauses; finding a post never enables a schedule.
                    g['next_at'] = time.time() + g['interval_hours'] * 3600
            self.busy.discard(gid)
            self.save()

    def claim(self, gid, scheduled=False):
        if self.api_only:
            raise ValueError(api_only.PENDING)
        with self.lock:
            if not self.operator_active():
                raise ValueError('Учётная запись оператора отключена.')
            g = self.group(gid)
            if not g:
                raise ValueError('Группа не найдена.')
            if g['mode'] not in ('api', 'api_suggest', 'browser_wall', 'browser_suggest'):
                raise ValueError('Для ручного режима подготовь пакет и заверши отправку на сайте VK.')
            if self.busy:
                raise ValueError('Идёт отправка. Дождись её завершения.')
            if self.pending_publication(gid):
                raise ValueError('Предыдущая новость ещё проверяется на стене. Повторная отправка приостановлена.')
            if g['mode'].startswith('browser_') and g.get('browser_checked') != g['mode']:
                raise ValueError('Сначала войди в браузере бота и проверь этот режим для группы.')
            if g['mode'] == 'api_suggest' and not g.get('api_checked'):
                raise ValueError('Сначала проверь подключение VK и группу.')
            if g['mode'] == 'api' and (not self.token or not self.account):
                raise ValueError('Сначала подключи аккаунт VK.')
            if g['mode'] == 'api' and not g.get('vk_id'):
                raise ValueError('Сначала проверь группу через VK.')
            if scheduled and (not self.operator_active() or self.state['automation_paused'] or not g['enabled'] or g['next_at'] > time.time()):
                return None
            draft = copy.deepcopy(self.state['draft'])
            if not draft['text'].strip() and not draft['photos']:
                raise ValueError('Объявление пустое.')
            job = {'id': uuid.uuid4().hex, 'group_id': gid, 'group_name': g['name'],
                   'time': time.time(), 'status': 'sending', 'detail': 'Отправляется', 'url': '',
                   'schedule_was_enabled': g['enabled']}
            if g['mode'] in ('browser_suggest', 'api_suggest'):
                job['publication'] = publication_watch(g, draft, job['time'])
                job['publication']['state'] = 'queued'
                job['publication']['vk_id'] = g.get('vk_id')
            self.add_history(job)
            # Persist BEFORE network I/O. Unfinished jobs are never resent on restart.
            g['next_at'] = time.time() + g['interval_hours'] * 3600
            self.save()
            self.busy.add(gid)
            return (copy.deepcopy(g), draft, job['id'], self.token)

    def deliver(self, task, client_class=VK):
        if self.api_only:
            raise ValueError(api_only.PENDING)
        g, draft, jid, token = task
        status, detail, url = 'sent', 'VK принял запись. Она может ожидать модерации.', ''
        posting = False
        try:
            if g['mode'].startswith('browser_'):
                posting = True
                def before_submit(known):
                    with self.lock:
                        h = next(h for h in self.state['history'] if h['id'] == jid)
                        h['publication']['known_posts'] = known
                        self.save()
                args = (before_submit,) if g['mode'] == 'browser_suggest' else ()
                result = self.browser.call('post', g, draft, self.folder / 'photos', *args)
                status, detail, url = result['status'], result['detail'], result['url']
            else:
                client = self.browser.client() if self.official else client_class(token)
                if self.official:
                    permission = client.group(g['slug'])
                    if permission.get('can_suggest') == 0:
                        with self.lock:
                            current = self.group(g['id'])
                            if current:
                                current.update(api_checked=False, api_error=SUGGEST_DENIED, can_suggest=0)
                        raise VKError(SUGGEST_DENIED)
                    if permission.get('can_post'):
                        raise VKError('VK разрешает прямую публикацию. Предложение новости не подтверждено; запись не отправлена.')
                    existing = client.call('wall.get', owner_id=-int(g['vk_id']), count=100, filter='all')
                    with self.lock:
                        h = next(h for h in self.state['history'] if h['id'] == jid)
                        h['publication']['known_posts'] = [f"-{g['vk_id']}_{p['id']}" for p in existing.get('items', [])]
                        self.save()
                attachments = [client.photo(self.folder / 'photos' / name) for name in draft['photos']]
                posting = True
                url = client.post(g['vk_id'], draft['text'], attachments, jid, layout=draft.get('photo_layout','grid')) if self.official else client.post(g['vk_id'], draft['text'], attachments, jid)
                if self.official:
                    status, detail = 'api_suggested', 'VK принял запись. Ожидается проверка публичной стены.'
                    with self.lock:
                        h = next(h for h in self.state['history'] if h['id'] == jid)
                        h['publication']['post_id'] = int(url.rsplit('_', 1)[1])
        except BrowserBusy as e:
            status, detail = 'deferred', str(e) + ' Отправка не начиналась.' + (' Повтор по расписанию через минуту.' if g['enabled'] else 'Повтори отправку позже.')
        except UncertainDelivery as e:
            status, detail = 'unknown', str(e)
        except VKError as e:
            status, detail = 'error', str(e)
        except Exception:
            status = 'unknown' if posting else 'error'
            detail = 'Нет подтверждения отправки. Проверь VK.' if posting else 'Ошибка подготовки фото. Запись не отправлена.'
        finally:
            with self.lock:
                h = next(h for h in self.state['history'] if h['id'] == jid)
                h.update(status=status, detail=detail, url=url)
                if h.get('publication'):
                    h['publication']['state'] = 'pending' if status in ('api_suggested', 'browser_suggested', 'unknown') else 'stopped'
                    if h['publication']['state'] == 'stopped':
                        h['publication']['detail'] = 'Запись не отправлена. Проверка стены не требуется.' if not posting else detail
                current = self.group(g['id'])
                if current:
                    current['next_at'] = time.time() + current['interval_hours'] * 3600
                    if status == 'deferred':
                        current['next_at'] = time.time() + 60
                    elif status not in ('sent', 'api_suggested', 'browser_posted', 'browser_suggested'):
                        if status == 'unknown' and current['enabled']:
                            current['delivery_pause'] = jid
                        current['enabled'] = False
                self.busy.discard(g['id'])
                self.save()

    def prepare(self, gid):
        with self.lock:
            g = self.group(gid)
            if not g or g['mode'] not in ('manual_wall', 'manual_suggest'):
                raise ValueError('Выбери ручной режим для этой группы.')
            if self.pending_publication(gid):
                raise ValueError('Предыдущая новость ещё проверяется на стене.')
            existing = next((t for t in self.state['tasks'] if t['group_id'] == gid), None)
            if existing:
                return existing
            draft = copy.deepcopy(self.state['draft'])
            if not draft['text'].strip() and not draft['photos']:
                raise ValueError('Сначала сохрани объявление.')
            task = {'id': uuid.uuid4().hex, 'group_id': gid, 'group_name': g['name'],
                    'url': g['url'], 'mode': g['mode'], 'time': time.time(), 'draft': draft}
            self.state['tasks'].append(task)
            self.save()
            return task

    def tick(self):
        if self.api_only:
            return
        with self.lock:
            if self.busy or not self.operator_active() or self.state['automation_paused']:
                return
            publication_due = next((h['id'] for h in self.state['history']
                                    if watching(h) and h['publication']['next_check'] <= time.time()), None)
            due = next((g['id'] for g in self.state['groups'] if g['enabled'] and not self.pending_publication(g['id']) and g['next_at'] <= time.time() and (
                (self.official and g['mode'] == 'api_suggest' and g.get('api_checked')) or (g['mode'] == 'browser_suggest' and g.get('browser_checked') == g['mode'])
            )), None)
        if publication_due:
            try:
                task = self.claim_publication(publication_due, automatic=True)
                if task:
                    self.check_publication(task)
            except ValueError:
                pass
        elif due:
            try:
                task = self.claim(due, scheduled=True)
                if task:
                    self.deliver(task)
            except ValueError:
                pass


def create_app(folder=None, browser_pool=None, official=False):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024 * 1024, TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'] + [host.strip() for host in os.environ.get('VK_POSTER_PUBLIC_HOST', '').split(',') if host.strip()])
    store = Store(folder or ROOT / 'data', browser_pool=browser_pool, official=official)
    app.store = store
    csrf = secrets.token_urlsafe(32)

    @app.before_request
    def security():
        if request.method != 'GET':
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf):
                abort(403)
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                abort(403)

    @app.before_request
    def transport_guard():
        if store.official:
            path = request.path
            if path in ('/vk-login', '/vk-connect'):
                return redirect('/vk-id')
            if path.startswith('/api/browser/') or path.endswith('/browser-check') or path.startswith('/api/tasks/') or path.endswith('/prepare'):
                return jsonify(error='Старый браузерный режим отключён. Используй VK ID и проверку группы.'), 410
        if not store.api_only:
            return
        path = request.path
        if path.startswith('/api/browser/') or path.endswith('/browser-check'):
            return jsonify(error='Браузер VK полностью отключён. Используется подключение через API.'), 410
        if path == '/vk-login':
            return render_template('vk-connect.html', integration=api_only.info())
        blocked = (path == '/api/connect' or path.endswith('/send') or
                   '/publication/check' in path or '/publication/start' in path or
                   path.startswith('/api/tasks/') or path.endswith('/prepare') or
                   (path.startswith('/api/groups/') and path.endswith('/check')))
        if request.method != 'GET' and blocked:
            return jsonify(error=api_only.PENDING), 409
        if path == '/api/automation' and request.method == 'POST' and request.json.get('running'):
            return jsonify(error=api_only.PENDING), 409

    @app.get('/vk-connect')
    def api_connection():
        if not store.api_only:
            return redirect('/vk-login')
        return render_template('vk-connect.html', integration=api_only.info())

    @app.after_request
    def headers(response):
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.errorhandler(ValueError)
    @app.errorhandler(VKError)
    def bad_input(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error={403: 'Обнови страницу и повтори действие.', 413: 'Размер фото должен быть меньше 15 МБ.'}.get(e.code, 'Запрос не принят.')), e.code

    @app.get('/')
    def index():
        return render_template('index.html', csrf=csrf, operator_name=app.config.get('OPERATOR_NAME'), operator_admin=app.config.get('OPERATOR_ADMIN',False))

    @app.get('/api/health')
    def health():
        return jsonify(app='vk-poster', instance=instance_id(folder or ROOT / 'data'))

    @app.get('/api/state')
    def state():
        with store.lock:
            return jsonify(**copy.deepcopy(store.state), account=store.account, vkid=store.connection_status(), busy=list(store.busy), browser=store.browser.info(), integration=api_only.info() if store.api_only else None, automation=store.automation_info())

    @app.post('/api/automation')
    def automation():
        running = request.json.get('running')
        if type(running) is not bool:
            raise ValueError('Укажи, нужно ли запустить автоматику.')
        with store.lock:
            info = store.automation_info()
            if running and not (info['schedules'] or info['publication_checks']):
                raise ValueError('Сначала настрой и включи расписание хотя бы одной проверенной группы.')
            store.state['automation_paused'] = not running
            store.save()
        return jsonify(automation=store.automation_info())

    @app.get('/vk-login')
    def vk_login():
        return render_template('vk-desktop.html' if os.environ.get('VK_BROWSER_DESKTOP') == '1' else 'vk-login.html', csrf=csrf)

    @app.post('/api/browser/connect')
    def browser_connect():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения операции.')
            store.busy.add('__login_open__')
        try:
            return jsonify(browser=store.browser.call('connect'))
        finally:
            with store.lock:
                store.busy.discard('__login_open__')

    @app.post('/api/browser/connect-status')
    def browser_connect_status():
        return jsonify(store.browser.call('login_progress'))

    @app.post('/api/browser/desktop')
    def desktop_start():
        return jsonify(store.browser.call('screen_start'))

    @app.post('/api/browser/desktop-heartbeat')
    def desktop_heartbeat():
        return jsonify(store.browser.call('desktop_heartbeat'))

    @app.post('/api/browser/desktop-verify')
    def desktop_verify():
        return jsonify(store.browser.call('desktop_verify'))

    @app.post('/api/browser/desktop-release')
    def desktop_release():
        return jsonify(store.browser.call('desktop_release'))

    @app.post('/api/browser/desktop-close')
    def desktop_close():
        return jsonify(store.browser.call('screen_close'))

    @app.post('/api/browser/screen')
    def browser_screen():
        if not store.browser.pool:
            raise ValueError('Экран доступен через кабинет оператора.')
        return Response(store.browser.call('remote_screen'), mimetype='image/jpeg')

    @app.post('/api/browser/input-batch')
    def browser_input_batch():
        if not store.browser.pool or not isinstance(request.json, dict):
            raise ValueError('Некорректный ввод.')
        return jsonify(store.browser.call('remote_batch', request.json.get('events')))

    @app.post('/api/browser/input')
    def browser_input():
        if not store.browser.pool or not isinstance(request.json, dict):
            raise ValueError('Некорректный ввод.')
        return jsonify(store.browser.call('remote_input', request.json))

    @app.post('/api/browser/reconnect')
    def browser_reconnect():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения операции.')
        return jsonify(browser=store.browser.call('reconnect'))

    @app.post('/api/browser/finish')
    def browser_finish():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения операции.')
        return jsonify(browser=store.browser.call('finish_login'))

    @app.post('/api/browser/close')
    def browser_close():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения операции.')
        return jsonify(browser=store.browser.call('close'))

    @app.post('/api/browser/open')
    def browser_open():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения отправки.')
            store.busy.add('__login_open__')
        try:
            return jsonify(browser=store.browser.call('open'))
        finally:
            with store.lock:
                store.busy.discard('__login_open__')

    @app.post('/api/groups/<gid>/browser-check')
    def browser_check(gid):
        with store.lock:
            g = copy.deepcopy(store.group(gid))
            if not g or g['mode'] != 'browser_suggest':
                raise ValueError('Выбери автоматическое предложение новости.')
            if store.busy:
                raise ValueError('Дождись завершения отправки.')
            draft = copy.deepcopy(store.state['draft'])
            store.busy.add(gid)
        try:
            result = store.browser.call('inspect', g, draft, store.folder / 'photos')
        except BrowserBusy:
            raise
        except VKError as error:
            with store.lock:
                current = store.group(gid)
                if current and current['mode'] == g['mode']:
                    current.pop('browser_checked', None)
                    current['browser_error'] = str(error)
                    current['enabled'] = False
                    store.save()
            raise
        finally:
            with store.lock:
                store.busy.discard(gid)
        with store.lock:
            current = store.group(gid)
            if not current or current['mode'] != g['mode']:
                raise ValueError('Настройки группы изменились. Повтори проверку.')
            current['browser_checked'] = g['mode']
            current.pop('browser_error', None)
            store.save()
        return jsonify(**result)

    @app.post('/api/connect')
    def connect():
        token = request.json.get('token', '').strip() or store.token
        if not token:
            raise ValueError('Введи пользовательский токен VK с правами wall и photos.')
        account = VK(token).identity()
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения отправки.')
            # A different account must explicitly re-enable every schedule.
            if not store.account or store.account['id'] != account['id']:
                for g in store.state['groups']:
                    if g['mode'] == 'api':
                        g['enabled'] = False
            store.token, store.account = token, account
            store.save()
        return jsonify(account=account)

    @app.post('/api/disconnect')
    def disconnect():
        with store.lock:
            if store.busy:
                raise ValueError('Дождись завершения текущей отправки.')
            store.token, store.account = '', None
            for g in store.state['groups']:
                if g['mode'] == 'api':
                    g['enabled'] = False
            store.save()
        return jsonify(ok=True)

    @app.put('/api/draft')
    def draft():
        body = request.json
        text, photos = body.get('text'), body.get('photos')
        layout = body.get('photo_layout',store.state['draft'].get('photo_layout','grid'))
        if layout not in ('grid','carousel'):
            raise ValueError('Выбери формат фотографий: плитки или лента.')
        if not isinstance(text, str) or len(text) > 15000:
            raise ValueError('Текст должен быть не длиннее 15 000 символов.')
        if not isinstance(photos, list) or len(photos) > 10 or any(
            not isinstance(p, str) or not re.fullmatch(r'[a-f0-9]{32}\.jpg', p) or not (store.folder / 'photos' / p).is_file() for p in photos
        ):
            raise ValueError('Можно прикрепить до 10 загруженных фотографий.')
        with store.lock:
            if body.get('revision') != store.state['draft_revision']:
                return jsonify(error='Настройки изменились. Обнови данные и повтори сохранение.'), 409
            store.state['draft'] = {'text': text, 'photos': photos, 'photo_layout':layout}
            store.state['draft_revision'] += 1
            store.save()
            return jsonify(ok=True, draft_revision=store.state['draft_revision'])

    @app.post('/api/photos')
    def upload():
        file = request.files.get('photo')
        if not file:
            raise ValueError('Выбери фото.')
        try:
            raw = file.read(15 * 1024 * 1024 + 1)
            if len(raw) > 15 * 1024 * 1024:
                raise ValueError('Фото должно быть меньше 15 МБ.')
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > 40_000_000:
                    raise ValueError('Фото слишком большое: максимум 40 мегапикселей.')
                im = ImageOps.exif_transpose(im).convert('RGB')
                im.thumbnail((2560, 2560))
                name = uuid.uuid4().hex + '.jpg'
                im.save(store.folder / 'photos' / name, 'JPEG', quality=92)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError('Не удалось прочитать изображение. Выбери JPG, PNG или WebP.') from None
        return jsonify(name=name)

    @app.get('/photos/<name>')
    def photo(name):
        if not re.fullmatch(r'[a-f0-9]{32}\.jpg', name):
            abort(404)
        return send_from_directory(store.folder / 'photos', name)

    @app.post('/api/groups')
    def add_group():
        slug = group_slug(request.json.get('url'))
        with store.lock:
            if any(g['slug'].lower() == slug.lower() for g in store.state['groups']):
                raise ValueError('Эта группа уже добавлена.')
            gid = uuid.uuid4().hex
            store.state['groups'].append({'id': gid, 'slug': slug, 'name': slug,
                                         'url': 'https://vk.com/' + slug, 'vk_id': None,
                                         'mode': 'api_suggest' if store.official else 'api' if store.api_only else 'browser_suggest',
                                         'enabled': False, 'interval_hours': 168,
                                         'next_at': time.time() + 168 * 3600})
            store.save()
        return jsonify(ok=True, id=gid)

    @app.post('/api/groups/<gid>/check')
    def check_group(gid):
        with store.lock:
            g = store.group(gid)
            if not g:
                abort(404)
            if not store.official and not store.account:
                raise ValueError('Сначала подключи аккаунт VK.')
            slug, token = g['slug'], store.token
            if store.official:
                g['api_checked'] = False
                store.save()
        client = store.browser.client() if store.official else VK(token)
        if store.official:
            client.identity()
        result = client.group(slug)
        if store.official and result.get('can_suggest') == 0:
            with store.lock:
                current = store.group(gid)
                if current:
                    current.update(api_checked=False, api_error=SUGGEST_DENIED, can_suggest=0, enabled=False)
                    store.save()
            raise ValueError(SUGGEST_DENIED)
        if store.official and result.get('can_post'):
            raise ValueError('VK разрешает прямую публикацию в этом сообществе. Режим предложения не подтверждён; отправка отключена, чтобы не опубликовать запись сразу.')
        with store.lock:
            g = store.group(gid)
            if not g:
                abort(404)
            if any(other['id'] != gid and other.get('vk_id') == result['vk_id'] for other in store.state['groups']):
                raise ValueError('Эта группа уже есть в списке под другой ссылкой.')
            g.update(result)
            g['vk_name'] = result['name']
            if g.get('custom_name'):
                g['name'] = g['custom_name']
            if store.official:
                g.update(api_checked=True, api_error='', api_warning=('Документация VK описывает предложение новости для публичных страниц. Возможность предложения в эту группу подтвердится только при первой отправке; при отказе VK расписание остановится.' if result.get('type') != 'page' else ''))
            store.save()
        return jsonify(ok=True, message='Права API и данные группы проверены. Возможность предложения подтвердится при первой отправке.')

    @app.put('/api/groups/<gid>/name')
    def rename_group(gid):
        name = request.json.get('name')
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('Название в панели: от 1 до 100 символов.')
        with store.lock:
            group = store.group(gid)
            if not group:
                abort(404)
            group['custom_name'] = name.strip()
            group['name'] = group['custom_name']
            store.save()
        return jsonify(ok=True)

    @app.put('/api/groups/<gid>')
    def edit_group(gid):
        b = request.json
        try:
            hours = float(b['interval_hours'])
            next_at = float(b['next_at'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Проверь интервал и время первой отправки.') from None
        if not math.isfinite(hours) or not 1 <= hours <= 8760:
            raise ValueError('Интервал: от 1 часа до 365 дней.')
        if not math.isfinite(next_at) or not time.time() - 60 <= next_at <= time.time() + 366 * 86400:
            raise ValueError('Следующая отправка должна быть в будущем, в пределах года.')
        if type(b.get('enabled')) is not bool:
            raise ValueError('Укажи состояние расписания.')
        with store.lock:
            g = store.group(gid)
            if not g:
                abort(404)
            mode = b.get('mode', g['mode'])
            if store.api_only:
                if b['enabled'] and not g['enabled']:
                    raise ValueError(api_only.PENDING)
                g.update(interval_hours=hours, next_at=next_at, enabled=b['enabled'], mode='api')
                store.save()
                return jsonify(ok=True)
            if store.official:
                if mode != 'api_suggest':
                    raise ValueError('Используй официальное предложение новости через API.')
                if b['enabled'] and not g.get('api_checked'):
                    raise ValueError('Сначала проверь группу и подключение VK.')
            if b['enabled'] and mode != 'browser_suggest' and not store.official:
                raise ValueError('Доступно только автоматическое предложение новости.')
            if mode not in ('api_suggest', 'api', 'manual_wall', 'manual_suggest', 'browser_wall', 'browser_suggest'):
                raise ValueError('Неизвестный способ размещения.')
            if mode != g['mode'] and (gid in store.busy or any(t['group_id'] == gid for t in store.state['tasks'])):
                raise ValueError('Сначала заверши или отмени текущее задание.')
            if b['enabled'] and mode == 'api' and (not store.account or not g.get('vk_id')):
                raise ValueError('Подключи аккаунт и проверь группу перед включением.')
            if b['enabled'] and mode.startswith('browser_') and g.get('browser_checked') != mode:
                raise ValueError('Сначала сохрани режим, войди в браузере бота и проверь группу.')
            if b['enabled'] and not (store.state['draft']['text'].strip() or store.state['draft']['photos']):
                raise ValueError('Сначала сохрани объявление.')
            g.update(interval_hours=hours, next_at=next_at, enabled=b['enabled'], mode=mode)
            g.pop('delivery_pause', None)  # Explicit user settings override automatic restoration.
            store.save()
        return jsonify(ok=True)

    @app.delete('/api/groups/<gid>')
    def delete_group(gid):
        with store.lock:
            if gid in store.busy:
                raise ValueError('Дождись завершения отправки в эту группу.')
            store.state['groups'] = [g for g in store.state['groups'] if g['id'] != gid]
            store.state['tasks'] = [t for t in store.state['tasks'] if t['group_id'] != gid]
            for h in store.state['history']:
                if h['group_id'] == gid and watching(h):
                    h['publication'].update(state='stopped', detail='Проверка остановлена: группа удалена.')
            store.save()
        return jsonify(ok=True)

    @app.post('/api/groups/<gid>/send')
    def send(gid):
        with store.lock:
            g = store.group(gid)
            if not g or g['mode'] != ('api_suggest' if store.official else 'browser_suggest'):
                raise ValueError('Доступно только автоматическое предложение новости.')
        task = store.claim(gid)
        threading.Thread(target=store.deliver, args=(task,), daemon=True).start()
        return jsonify(ok=True), 202

    @app.post('/api/history/<jid>/publication/check')
    def publication_check(jid):
        task = store.claim_publication(jid)
        threading.Thread(target=store.check_publication, args=(task,), daemon=True).start()
        return jsonify(ok=True), 202

    @app.post('/api/history/<jid>/publication/start')
    def publication_start(jid):
        with store.lock:
            h = next((h for h in store.state['history'] if h['id'] == jid), None)
            if not h or h['status'] not in ('api_suggested', 'browser_suggested', 'manual_suggested', 'unknown'):
                raise ValueError('Для этой записи нельзя включить поиск публикации.')
            if h.get('publication'):
                raise ValueError('Проверка этой записи уже настроена.')
            g = store.group(h['group_id'])
            if not g:
                raise ValueError('Группа удалена. Добавь её для дальнейших отправок.')
            if store.busy or store.pending_publication(g['id']):
                raise ValueError('Дождись текущей проверки или отправки в эту группу.')
            if not request.json.get('use_current_draft'):
                raise ValueError('У старой отправки нет снимка объявления. Подтверди поиск по текущему тексту.')
            draft = store.state['draft']
            if not draft['text'].strip():
                raise ValueError('Для поиска нужен текст объявления.')
            h['publication'] = publication_watch(g, draft, h['time'], source='current_draft')
            h['publication']['next_check'] = time.time()
            store.save()
        return jsonify(ok=True)

    @app.post('/api/history/<jid>/publication/stop')
    def publication_stop(jid):
        with store.lock:
            h = next((h for h in store.state['history'] if h['id'] == jid), None)
            if not h or not watching(h):
                raise ValueError('Активная проверка не найдена.')
            if h['group_id'] in store.busy:
                raise ValueError('Дождись завершения текущей операции.')
            h['publication'].update(state='stopped', detail='Проверка остановлена пользователем.')
            g = store.group(h['group_id'])
            if g:
                g['enabled'] = False
            store.save()
        return jsonify(ok=True)

    @app.post('/api/pause')
    def pause():
        with store.lock:
            for g in store.state['groups']:
                g['enabled'] = False
                g.pop('delivery_pause', None)
            store.save()
        return jsonify(ok=True)

    @app.post('/api/groups/<gid>/prepare')
    def prepare(gid):
        return jsonify(task=store.prepare(gid))

    @app.get('/api/tasks/<tid>/package')
    def package(tid):
        with store.lock:
            task = copy.deepcopy(next((t for t in store.state['tasks'] if t['id'] == tid), None))
        if not task:
            abort(404)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('text.txt', task['draft']['text'])
            action = 'Нажми «Предложить новость», если эта кнопка доступна.' if task['mode'] == 'manual_suggest' else 'Открой форму новой записи на стене, если публикация доступна.'
            archive.writestr('instructions.txt', f"Группа: {task['url']}\n{action}\nВставь текст из text.txt и прикрепи фото по порядку.\nОтправь запись в VK, затем отметь результат в боте.\nЕсли формы нет, доступ к размещению нужно уточнить у администратора.\n")
            for i, name in enumerate(task['draft']['photos'], 1):
                archive.write(store.folder / 'photos' / name, f'photos/{i:02d}.jpg')
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name=f'vk-post-{tid[:8]}.zip', mimetype='application/zip')

    @app.post('/api/tasks/<tid>/finish')
    def finish(tid):
        result = request.json.get('result')
        if result not in ('done', 'cancelled'):
            raise ValueError('Выбери результат задания.')
        with store.lock:
            task = next((t for t in store.state['tasks'] if t['id'] == tid), None)
            if not task:
                abort(404)
            suggested = task['mode'] == 'manual_suggest'
            status = 'cancelled' if result == 'cancelled' else ('manual_suggested' if suggested else 'manual_posted')
            detail = 'Задание отменено пользователем.' if result == 'cancelled' else ('Пользователь отметил отправку на модерацию. Публикация не подтверждена.' if suggested else 'Пользователь отметил размещение на стене. Бот не проверял запись в VK.')
            job = {'id': tid, 'group_id': task['group_id'], 'group_name': task['group_name'],
                   'time': time.time(), 'status': status, 'detail': detail, 'url': ''}
            if suggested and result == 'done':
                job['publication'] = publication_watch({'url': task['url']}, task['draft'], task['time'], source='manual')
            store.add_history(job)
            store.state['tasks'] = [t for t in store.state['tasks'] if t['id'] != tid]
            g = store.group(task['group_id'])
            if g:
                g['next_at'] = time.time() + g['interval_hours'] * 3600
                if suggested and result == 'done':
                    # Do not prompt another submission while moderation is pending.
                    g['enabled'] = False
            store.save()
        return jsonify(ok=True)

    return app


def run():
    from waitress import serve
    folder = Path(os.environ.get('VK_POSTER_DATA', ROOT / 'data'))
    folder.mkdir(parents=True, exist_ok=True)
    process_lock = ProcessLock(folder / 'process.lock')
    try:
        process_lock.__enter__()
    except RuntimeError as error:
        raise SystemExit(str(error))
    def terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    from operators import create_gateway
    app = create_gateway(folder)
    stop = threading.Event()

    def scheduler():
        while not stop.is_set():
            try:
                app.workspaces.tick()
            except Exception:
                print('Ошибка планировщика. Проверь доступность папки данных.')
            stop.wait(10)

    worker = threading.Thread(target=scheduler, daemon=True)
    worker.start()
    port = int(os.environ.get('VK_POSTER_PORT', '8787'))
    print(f'VK Poster: http://127.0.0.1:{port}', flush=True)
    try:
        proxy = {'trusted_proxy': os.environ.get('VK_POSTER_TRUSTED_PROXY', '127.0.0.1'), 'trusted_proxy_headers': {'x-forwarded-for', 'x-forwarded-proto'}} if os.environ.get('VK_POSTER_PROXY') == '1' else {}
        serve(app, host=os.environ.get('VK_POSTER_BIND', '127.0.0.1'), port=port, threads=32, **proxy)
    finally:
        stop.set()
        worker.join(timeout=60)
        if not worker.is_alive():
            app.workspaces.pool.shutdown()
        process_lock.__exit__(None, None, None)


if __name__ == '__main__':
    run()
