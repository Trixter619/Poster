"""Official VK ID authorization. Credentials never enter browser responses or logs."""
import base64
import os
import hashlib
import json
import secrets
import time
import threading
from urllib.parse import urlencode, urlparse

import requests
from vk_api import VK, VKError
from flask import abort, g, redirect, render_template, request, session

APP_ID = os.environ.get('VKID_APP_ID', '54800785')
CALLBACK = os.environ.get('VKID_CALLBACK_URL', 'https://sagposter.duckdns.org/auth/vk/callback')
CALLBACK_ORIGIN = CALLBACK.rsplit('/auth/vk/callback', 1)[0]
AUTHORIZE = 'https://id.vk.ru/authorize'
TOKEN = 'https://id.vk.ru/oauth2/auth'


class AuthFailure(Exception):
    pass


class VKID:
    def __init__(self, users):
        self.users = users
        self.token_lock = threading.RLock()
        with users.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS vkid_flows (state_hash TEXT PRIMARY KEY, uid TEXT, version INTEGER, binding TEXT, verifier TEXT, expires REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS vkid_accounts (uid TEXT PRIMARY KEY, credentials TEXT NOT NULL, connected_at REAL NOT NULL)')

    def begin(self, user, binding):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        with self.users.connect() as db:
            db.execute('DELETE FROM vkid_flows WHERE expires < ? OR uid=?', (time.time(), user['id']))
            db.execute('INSERT INTO vkid_flows VALUES (?,?,?,?,?,?)',
                       (hashlib.sha256(state.encode()).hexdigest(), user['id'], user['version'], binding, verifier, time.time()+600))
        return AUTHORIZE + '?' + urlencode(dict(response_type='code', client_id=APP_ID,
            redirect_uri=CALLBACK, state=state, code_challenge=challenge,
            code_challenge_method='S256', scope='wall photos', prompt='consent'))

    def consume(self, user, binding, state):
        if not isinstance(state, str) or not 20 <= len(state) <= 256:
            raise AuthFailure('Вход не подтверждён. Начни подключение заново.')
        key = hashlib.sha256(state.encode()).hexdigest()
        with self.users.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            flow = db.execute('SELECT * FROM vkid_flows WHERE state_hash=?', (key,)).fetchone()
            if (not flow or flow['uid'] != user['id'] or flow['version'] != user['version']
                    or not binding or not secrets.compare_digest(flow['binding'], binding) or flow['expires'] < time.time()):
                raise AuthFailure('Ссылка входа устарела или открыта в другой сессии. Начни подключение заново.')
            db.execute('DELETE FROM vkid_flows WHERE state_hash=?', (key,))
        return flow['verifier']

    def exchange(self, user, code, device_id, state, verifier):
        if not code or not device_id or len(code) > 4096 or len(device_id) > 4096:
            raise AuthFailure('VK не вернул данные для завершения входа. Повтори подключение.')
        try:
            response = requests.post(TOKEN, data=dict(grant_type='authorization_code',
                client_id=APP_ID, redirect_uri=CALLBACK, code=code, device_id=device_id,
                state=state, code_verifier=verifier), timeout=(10, 25), allow_redirects=False)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            raise AuthFailure('Не удалось завершить обмен с VK ID. Повтори подключение.') from None
        if not isinstance(data, dict) or data.get('error'):
            raise AuthFailure('VK ID отклонил обмен кода. Проверь настройки приложения и повтори вход.')
        if not isinstance(data.get('state'), str) or not secrets.compare_digest(data['state'], state):
            raise AuthFailure('Ответ VK ID не соответствует запросу. Повтори подключение.')
        access, refresh, uid = data.get('access_token'), data.get('refresh_token'), str(data.get('user_id', ''))
        try:
            expires = int(data.get('expires_in', 0))
        except (TypeError, ValueError):
            expires = 0
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh or not uid.isdigit() or int(uid) <= 0 or not 0 < expires <= 86400:
            raise AuthFailure('VK ID вернул неполный результат. Подключение не сохранено.')
        scope = data.get('scope', '')
        if not isinstance(scope, str):
            scope = ''
        credentials = dict(access_token=access, refresh_token=refresh, user_id=uid,
                           device_id=device_id, scope=scope, expires_at=time.time()+expires)
        with self.users.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT active,version FROM users WHERE id=?', (user['id'],)).fetchone()
            if not current or not current['active'] or current['version'] != user['version']:
                raise AuthFailure('Сессия оператора завершена. Войди в бот заново.')
            db.execute('INSERT OR REPLACE INTO vkid_accounts VALUES (?,?,?)',
                       (user['id'], json.dumps(credentials), time.time()))

    def access(self, uid, force_refresh=False):
        # Serialize refresh rotation; credentials never leave the server process.
        with self.token_lock:
            user = self.users.user(uid)
            if not user or not user['active']:
                raise VKError('Учётная запись оператора отключена.')
            with self.users.connect() as db:
                row = db.execute('SELECT credentials FROM vkid_accounts WHERE uid=?', (uid,)).fetchone()
            if not row:
                raise VKError('Подключи VK ID перед отправкой.')
            data = json.loads(row['credentials'])
            if not {'wall', 'photos'} <= set(data.get('scope', '').split()):
                raise VKError('Подключи VK заново и разреши доступ к стене и фотографиям.')
            if force_refresh or data['expires_at'] <= time.time() + 120:
                state = secrets.token_urlsafe(32)
                try:
                    r = requests.post(TOKEN, data=dict(grant_type='refresh_token',
                        client_id=APP_ID, refresh_token=data['refresh_token'],
                        device_id=data['device_id'], state=state), timeout=(10,25), allow_redirects=False)
                    r.raise_for_status()
                    result = r.json()
                except (requests.RequestException, ValueError):
                    raise VKError('Не удалось обновить подключение VK. Повтори проверку позже.') from None
                if not isinstance(result, dict) or result.get('error'):
                    raise VKError('VK отклонил продление подключения. Подключи VK заново.')
                if not isinstance(result.get('state'), str) or not secrets.compare_digest(state, result['state']):
                    raise VKError('Ответ продления VK не соответствует запросу.')
                try:
                    expires = int(result.get('expires_in', 0))
                except (TypeError, ValueError):
                    expires = 0
                if (not isinstance(result.get('access_token'), str) or not result['access_token']
                    or not isinstance(result.get('refresh_token'), str) or not result['refresh_token']
                    or not 0 < expires <= 86400
                    or str(result.get('user_id', data['user_id'])) != str(data['user_id'])):
                    raise VKError('VK вернул неполный результат продления. Подключи VK заново.')
                scope = result.get('scope', data['scope'])
                if not isinstance(scope, str) or not {'wall', 'photos'} <= set(scope.split()):
                    raise VKError('VK не подтвердил права wall и photos при продлении.')
                data.update(access_token=result['access_token'], refresh_token=result['refresh_token'],
                            scope=scope, expires_at=time.time()+expires)
                with self.users.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT active,version FROM users WHERE id=?', (uid,)).fetchone()
                    if not current or not current['active'] or current['version'] != user['version']:
                        raise VKError('Сессия оператора завершена.')
                    changed = db.execute('UPDATE vkid_accounts SET credentials=? WHERE uid=? AND credentials=?',
                        (json.dumps(data), uid, row['credentials'])).rowcount
                    if not changed:
                        raise VKError('Подключение VK изменилось. Повтори проверку.')
            return data['access_token']

    def client(self, uid):
        auth = self
        class OperatorVK(VK):
            def call(self, method, **params):
                self.token = auth.access(uid)
                return super().call(method, **params)
        return OperatorVK('')

    def status(self, uid):
        # Explicit allowlist: never serialize stored credentials to client.
        with self.users.connect() as db:
            row = db.execute('SELECT credentials FROM vkid_accounts WHERE uid=?', (uid,)).fetchone()
        if not row:
            return None
        data = json.loads(row['credentials'])
        return dict(user_id=data['user_id'], scope=data['scope'], expired=data['expires_at'] <= time.time())


def install(app, users):
    auth = VKID(users)
    app.vkid = auth

    @app.get('/vk-id')
    def vkid_page():
        message = session.pop('vkid_message', '')
        error = session.pop('vkid_error', '')
        return render_template('vk-id.html', status=auth.status(g.operator['id']),
                               csrf=session['csrf'], message=message, error=error,
                               canonical=request.host_url.rstrip('/') != CALLBACK_ORIGIN,
                               canonical_url=CALLBACK_ORIGIN + '/vk-id')

    @app.post('/auth/vk/start')
    def vkid_start():
        if not secrets.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '!'):
            abort(403)
        if request.host_url.rstrip('/') != CALLBACK_ORIGIN:
            return redirect(CALLBACK_ORIGIN + '/vk-id', code=303)
        binding = secrets.token_urlsafe(32)
        session['vkid_binding'] = binding
        return redirect(auth.begin(g.operator, binding), code=303)

    @app.get('/auth/vk/callback')
    def vkid_callback():
        try:
            state = request.args.get('state', '')
            verifier = auth.consume(g.operator, session.get('vkid_binding', ''), state)
            session.pop('vkid_binding', None)
            if request.args.get('error'):
                raise AuthFailure('Вход в VK отменён или доступ не предоставлен.')
            auth.exchange(g.operator, request.args.get('code', ''), request.args.get('device_id', ''), state, verifier)
            session['vkid_message'] = 'Вход через VK ID выполнен. Подключение сохранено для твоей учётной записи оператора.'
        except AuthFailure as exc:
            session['vkid_error'] = str(exc)
        return redirect('/vk-id', code=303)
