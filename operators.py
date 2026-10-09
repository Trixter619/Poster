"""Authenticated gateway. Tenant selection comes only from the signed session."""
import os
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import timedelta
from contextlib import contextmanager

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response
from werkzeug.middleware.proxy_fix import ProxyFix

import api_only
from launcher import instance_id


class Operators:
    def __init__(self, folder):
        self.root = Path(folder).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'operators.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL, admin INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1, workspace TEXT NOT NULL UNIQUE)')
            columns = {r[1] for r in db.execute('PRAGMA table_info(users)')}
            if 'approval' not in columns:
                db.execute("ALTER TABLE users ADD COLUMN approval TEXT NOT NULL DEFAULT 'approved'")
            db.execute('CREATE TABLE IF NOT EXISTS access_audit (actor TEXT, target TEXT, action TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS registration_attempts (ip TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (ip TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS password_requests (uid TEXT PRIMARY KEY, at REAL, status TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS recovery_attempts (ip TEXT, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS password_resets (digest TEXT PRIMARY KEY, uid TEXT UNIQUE, version INTEGER, expires REAL)')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_time ON attempts(at)')
        os.chmod(self.db, 0o600)
        self.dummy = generate_password_hash(secrets.token_hex(32))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def users(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,admin,active,version,workspace,approval FROM users ORDER BY rowid')]

    def user(self, uid):
        with self.connect() as db:
            row = db.execute('SELECT id,name,admin,active,version,workspace,approval FROM users WHERE id=?', (uid,)).fetchone()
            return dict(row) if row else None

    def add(self, name, password, first=False, pending=False, actor=None):
        name = name.strip().lower()
        if not re.fullmatch(r'[a-z0-9_.-]{3,40}', name):
            raise ValueError('Логин: 3–40 латинских букв, цифр, точек, дефисов или подчёркиваний.')
        if not 12 <= len(password) <= 256:
            raise ValueError('Пароль должен содержать от 12 до 256 символов.')
        hashed = generate_password_hash(password)
        uid = uuid.uuid4().hex
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                exists = db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is not None
                if first == exists:
                    raise ValueError('Первый администратор уже создан.' if first else 'Сначала создай администратора.')
                if pending and db.execute("SELECT count(*) FROM users WHERE approval='pending'").fetchone()[0] >= 100:
                    raise ValueError('Очередь заявок заполнена. Обратись к администратору.')
                db.execute('INSERT INTO users (id,name,password,admin,workspace,active,approval) VALUES (?,?,?,?,?,?,?)',
                           (uid, name, hashed, int(first), '.' if first else 'operators/' + uid, int(not pending), 'pending' if pending else 'approved'))
                db.execute('INSERT INTO access_audit VALUES (?,?,?,?)', (uid if first or pending else actor, uid, 'request' if pending else 'create', time.time()))
        except sqlite3.IntegrityError:
            raise ValueError('Такой логин уже существует.') from None
        return uid

    def authenticate(self, name, password, ip):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE at < ?', (now - 300,))
            count = db.execute('SELECT count(*) FROM attempts WHERE ip=?', (ip,)).fetchone()[0]
            if count >= 10:
                raise ValueError('Слишком много попыток. Повтори через пять минут.')
            db.execute('INSERT INTO attempts VALUES (?,?)', (ip, now))
            row = db.execute('SELECT * FROM users WHERE name=?', (name.strip().lower(),)).fetchone()
        valid = check_password_hash(row['password'] if row else self.dummy, password[:257]) and len(password) <= 256
        if not row or not valid:
            raise ValueError('Неверный логин или пароль.')
        if row['approval'] == 'pending':
            raise ValueError('Заявка ожидает одобрения администратора.')
        if not row['active'] or row['approval'] != 'approved':
            raise ValueError('Доступ не выдан или отключён. Обратись к администратору.')
        with self.connect() as db:
            db.execute('DELETE FROM attempts WHERE ip=?', (ip,))
        return self.user(row['id'])

    def invalidate(self, uid):
        with self.connect() as db:
            db.execute('UPDATE users SET version=version+1 WHERE id=?', (uid,))

    def toggle(self, uid, actor=None):
        with self.connect() as db:
            db.execute("UPDATE users SET active=1-active, version=version+1 WHERE id=? AND admin=0 AND approval='approved'", (uid,))
            row = db.execute('SELECT active FROM users WHERE id=?', (uid,)).fetchone()
            if row:
                db.execute('INSERT INTO access_audit VALUES (?,?,?,?)', (actor, uid, 'enable' if row['active'] else 'disable', time.time()))

    def registration_limit(self, ip):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM registration_attempts WHERE at < ?', (now - 3600,))
            if db.execute('SELECT count(*) FROM registration_attempts WHERE ip=?', (ip,)).fetchone()[0] >= 5:
                raise ValueError('Слишком много заявок. Повтори через час.')
            db.execute('INSERT INTO registration_attempts VALUES (?,?)', (ip, now))

    def decide(self, uid, actor, approve):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            admin = db.execute('SELECT admin,active FROM users WHERE id=?', (actor,)).fetchone()
            if not admin or not admin['admin'] or not admin['active']:
                raise ValueError('Одобрить заявку может только администратор.')
            result = db.execute("UPDATE users SET approval=?,active=?,version=version+1 WHERE id=? AND admin=0 AND approval='pending'",
                                ('approved' if approve else 'rejected', int(approve), uid))
            if result.rowcount != 1:
                raise ValueError('Заявка уже рассмотрена или не найдена.')
            db.execute('INSERT INTO access_audit VALUES (?,?,?,?)',
                       (actor, uid, 'approve' if approve else 'reject', time.time()))

    def audit(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT actor.name AS actor, target.name AS target, a.action, strftime('%Y-%m-%d %H:%M', a.at, 'unixepoch') AS at FROM access_audit a LEFT JOIN users actor ON actor.id=a.actor LEFT JOIN users target ON target.id=a.target ORDER BY a.rowid DESC LIMIT 50")]

    def change_password(self, uid, password):
        if not 12 <= len(password) <= 256:
            raise ValueError('Пароль должен содержать от 12 до 256 символов.')
        with self.connect() as db:
            db.execute('UPDATE users SET password=?, version=version+1 WHERE id=?',
                       (generate_password_hash(password), uid))

    def set_admin(self, uid, actor, enabled):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            admin = db.execute('SELECT admin,active FROM users WHERE id=?', (actor,)).fetchone()
            target = db.execute('SELECT admin,active,approval FROM users WHERE id=?', (uid,)).fetchone()
            if not admin or not admin['admin'] or not admin['active']:
                raise ValueError('Изменять права может только администратор.')
            if not target or not target['active'] or target['approval'] != 'approved':
                raise ValueError('Права можно изменить только у активного оператора с одобренным доступом.')
            if bool(target['admin']) == enabled:
                return
            if not enabled and db.execute("SELECT count(*) FROM users WHERE admin=1 AND active=1 AND approval='approved'").fetchone()[0] <= 1:
                raise ValueError('Нельзя снять права последнего активного администратора.')
            db.execute('UPDATE users SET admin=?,version=version+1 WHERE id=?', (int(enabled),uid))
            db.execute('DELETE FROM password_resets WHERE uid=?', (uid,))
            db.execute('INSERT INTO access_audit VALUES (?,?,?,?)', (actor,uid,'admin_granted' if enabled else 'admin_revoked',time.time()))

    def request_recovery(self, name, ip):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM recovery_attempts WHERE at < ?', (now-3600,))
            if db.execute('SELECT count(*) FROM recovery_attempts WHERE ip=?', (ip,)).fetchone()[0] >= 5:
                raise ValueError('Слишком много запросов. Повтори через час.')
            db.execute('INSERT INTO recovery_attempts VALUES (?,?)', (ip,now))
            user = db.execute("SELECT id FROM users WHERE name=? AND active=1 AND approval='approved'", (name.strip().lower(),)).fetchone()
            if user:
                db.execute("INSERT INTO password_requests VALUES (?,?,'pending') ON CONFLICT(uid) DO UPDATE SET at=excluded.at,status='pending'", (user['id'],now))

    def recovery_requests(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT u.id,u.name,p.status FROM password_requests p JOIN users u ON u.id=p.uid WHERE u.active=1 AND u.approval='approved' ORDER BY p.at")]

    def issue_reset(self, uid, actor):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            admin = db.execute('SELECT admin,active FROM users WHERE id=?', (actor,)).fetchone()
            target = db.execute('SELECT admin,active,approval,version FROM users WHERE id=?', (uid,)).fetchone()
            if not admin or not admin['admin'] or not admin['active']:
                raise ValueError('Сброс разрешён только администратору.')
            if not target or uid == actor or not target['active'] or target['approval'] != 'approved':
                raise ValueError('Сброс доступен только активному оператору с одобренным доступом.')
            db.execute('DELETE FROM password_resets WHERE uid=? OR expires<?', (uid,time.time()))
            db.execute('INSERT INTO password_resets VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),uid,target['version'],time.time()+1800))
            db.execute("UPDATE password_requests SET status='issued' WHERE uid=?", (uid,))
            db.execute('INSERT INTO access_audit VALUES (?,?,?,?)', (actor,uid,'password_reset_issued',time.time()))
        return token

    def reset_password(self, token, password):
        if not isinstance(token,str) or not 40 <= len(token) <= 128:
            raise ValueError('Ссылка недействительна. Запроси новую у администратора.')
        if not 12 <= len(password) <= 256:
            raise ValueError('Пароль должен содержать от 12 до 256 символов.')
        hashed = generate_password_hash(password)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM password_resets WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            user = db.execute('SELECT * FROM users WHERE id=?', (row['uid'],)).fetchone() if row else None
            if (not row or row['expires'] < time.time() or not user or not user['active']
                or user['approval'] != 'approved' or user['version'] != row['version']):
                raise ValueError('Ссылка истекла или уже использована. Запроси новую у администратора.')
            db.execute('UPDATE users SET password=?,version=version+1 WHERE id=?', (hashed,user['id']))
            db.execute('DELETE FROM password_resets WHERE uid=?', (user['id'],))
            db.execute('DELETE FROM password_requests WHERE uid=?', (user['id'],))
            db.execute('INSERT INTO access_audit VALUES (?,?,?,?)', (user['id'],user['id'],'password_reset_completed',time.time()))



class Workspaces:
    def __init__(self, users):
        self.users = users
        self.pool = api_only.ApiRuntime()
        from vkid_auth import VKID
        self.vkid = VKID(users)
        self.apps = {}
        self.lock = threading.RLock()
        self.cursor = 0

    def get(self, user):
        from app import create_app
        with self.lock:
            if user['id'] not in self.apps:
                app = create_app(self.users.root / user['workspace'], browser_pool=self.pool, official=True)
                # Never inherit one environment token into another operator's account.
                app.store.token = ''
                app.store.browser.factory = lambda uid=user['id']: self.vkid.client(uid)
                app.store.connection_status = lambda uid=user['id']: self.vkid.status(uid)
                app.store.operator_active = lambda uid=user['id']: bool((self.users.user(uid) or {}).get('active'))
                app.store.recover()
                app.config['OPERATOR_NAME'] = user['name']
                app.config['OPERATOR_ADMIN'] = bool(user['admin'])
                self.apps[user['id']] = app
            self.apps[user['id']].config['OPERATOR_ADMIN'] = bool(user['admin'])
            return self.apps[user['id']]

    def tick(self):
        users = [u for u in self.users.users() if u['active']]
        # Round-robin order avoids starving later operators when many jobs are due.
        if users:
            self.cursor %= len(users)
            users = users[self.cursor:] + users[:self.cursor]
            self.cursor += 1
        for user in users:
            app = self.get(user)
            if self.pool.available(app.store.browser):
                try:
                    app.store.tick()
                except Exception:
                    print('Ошибка задачи оператора. Подробности доступны в его журнале.', flush=True)
        self.pool.collect()


def create_gateway(folder):
    users = Operators(folder)
    hub = Workspaces(users)
    app = Flask(__name__)
    key_path = users.root / 'session.key'
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as f:
            f.write(secrets.token_bytes(32))
    app.config.update(SECRET_KEY=key_path.read_bytes(), SESSION_COOKIE_NAME='vkposter_session',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('VK_POSTER_HTTPS') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                      MAX_CONTENT_LENGTH=16 * 1024 * 1024,
                      TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'] + [host.strip() for host in os.environ.get('VK_POSTER_PUBLIC_HOST', '').split(',') if host.strip()])
    if os.environ.get('VK_POSTER_PROXY') == '1':
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    app.operators = users
    app.workspaces = hub

    @app.before_request
    def guard():
        g.operator = users.user(session.get('uid'))
        if g.operator and (not g.operator['active'] or g.operator['version'] != session.get('version')):
            session.clear()
            g.operator = None
        if request.path == '/api/health':
            return None
        # Only setup/login and CSS are public; API/photos always require login.
        public = request.path in ('/api/health', '/login', '/setup', '/register', '/forgot-password', '/reset-password', '/static/style.css', '/static/reset-password.js')
        if not public and not g.operator:
            if request.path.startswith(('/api/', '/photos/')):
                return jsonify(error='Войди в учётную запись оператора.'), 401
            return redirect('/login')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                abort(403)
            if request.path in ('/login', '/setup', '/register', '/forgot-password', '/reset-password', '/account', '/admin', '/logout'):
                if not secrets.compare_digest(request.form.get('csrf', ''), session.get('csrf', '') or '!'):
                    abort(403)
        session.setdefault('csrf', secrets.token_urlsafe(32))

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if request.path in ('/vk-id', '/auth/vk/start'):
            response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace("form-action 'self'", "form-action 'self' https://id.vk.ru")
        if request.path == '/auth/vk/callback':
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    from vkid_auth import install as install_vkid
    install_vkid(app, users)

    @app.get('/api/health')
    def health():
        return jsonify(app='vk-poster', instance=instance_id(folder))

    @app.route('/setup', methods=['GET', 'POST'])
    def setup():
        if os.environ.get('VK_POSTER_PUBLIC_HOST'):
            abort(403)
        if users.users():
            return redirect('/login')
        if request.remote_addr not in ('127.0.0.1', '::1'):
            abort(403)
        error = ''
        if request.method == 'POST':
            try:
                uid = users.add(request.form.get('username', ''), request.form.get('password', ''), first=True)
                session.clear()
                session.update(uid=uid, version=1, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect('/')
            except ValueError as exc:
                error = str(exc)
        return render_template('login.html', setup=True, error=error, csrf=session['csrf'])

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not users.users():
            return redirect('/setup')
        if g.operator:
            return redirect('/')
        error = ''
        if request.method == 'POST':
            try:
                user = users.authenticate(request.form.get('username', ''), request.form.get('password', ''), request.remote_addr or 'unknown')
                session.clear()
                session.update(uid=user['id'], version=user['version'], csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect('/')
            except ValueError as exc:
                error = str(exc)
        return render_template('login.html', setup=False, error=error, csrf=session['csrf'])

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if not users.users():
            abort(404)
        error, message = '', ''
        if request.method == 'POST':
            try:
                users.registration_limit(request.remote_addr or 'unknown')
                users.add(request.form.get('username', ''), request.form.get('password', ''), pending=True)
                message = 'Заявка отправлена. Войти можно после одобрения администратором.'
            except ValueError as exc:
                error = str(exc)
        return render_template('register.html', error=error, message=message, csrf=session['csrf'])

    @app.route('/forgot-password', methods=['GET','POST'])
    def forgot_password():
        message, error = '', ''
        if request.method == 'POST':
            try:
                users.request_recovery(request.form.get('username',''), request.remote_addr or 'unknown')
                message = 'Если это активная учётная запись оператора, заявка появится у администратора. Получи у него ссылку восстановления лично.'
            except ValueError as exc:
                error = str(exc)
        return render_template('forgot-password.html',csrf=session['csrf'],message=message,error=error)

    @app.route('/reset-password', methods=['GET','POST'])
    def reset_password():
        error, completed = '', False
        if request.method == 'POST':
            try:
                users.request_recovery('', request.remote_addr or 'unknown')
                if request.form.get('password') != request.form.get('password_confirm'):
                    raise ValueError('Новые пароли не совпадают.')
                users.reset_password(request.form.get('token',''),request.form.get('password',''))
                session.clear()
                completed = True
            except ValueError as exc:
                error = str(exc)
        return render_template('reset-password.html',csrf=session.get('csrf',''),error=error,completed=completed,token=request.form.get('token','') if not completed else '')

    @app.get('/desktop-client/<path:asset>')
    @app.get('/api/desktop-access')
    def desktop_retired(asset=None):
        abort(410)

    def revoke_screen(user):
        pass  # No browser processes or screens exist in the API runtime.

    @app.post('/logout')
    def logout():
        revoke_screen(g.operator)
        users.invalidate(g.operator['id'])
        session.clear()
        return redirect('/login')

    @app.route('/admin', methods=['GET', 'POST'])
    @app.route('/account', methods=['GET', 'POST'])
    def account():
        admin_view = request.path == '/admin'
        if admin_view and not g.operator['admin']:
            abort(403)
        reset_link = ''
        error, message = '', ''
        if request.method == 'POST':
            action = request.form.get('action')
            try:
                if action == 'password':
                    if request.form.get('password') != request.form.get('password_confirm'):
                        raise ValueError('Новые пароли не совпадают.')
                    user = users.authenticate(g.operator['name'], request.form.get('old_password', ''), request.remote_addr)
                    revoke_screen(user)
                    users.change_password(user['id'], request.form.get('password', ''))
                    session.clear()
                    return redirect('/login')
                if not g.operator['admin']:
                    abort(403)
                if action == 'add':
                    users.add(request.form.get('username', ''), request.form.get('password', ''), actor=g.operator['id'])
                    message = 'Оператор добавлен. Передай ему логин и пароль безопасным способом.'
                elif action in ('approve', 'reject'):
                    users.decide(request.form.get('uid', ''), g.operator['id'], action == 'approve')
                    message = 'Доступ одобрен.' if action == 'approve' else 'Заявка отклонена.'
                elif action in ('grant_admin','revoke_admin'):
                    uid = request.form.get('uid','')
                    users.set_admin(uid,g.operator['id'],action == 'grant_admin')
                    if uid == g.operator['id']:
                        session.clear()
                        return redirect('/login')
                    message = 'Права администратора выданы. Оператору нужно войти заново.' if action == 'grant_admin' else 'Права администратора сняты. Оператору нужно войти заново.'
                elif action == 'reset_password':
                    token = users.issue_reset(request.form.get('uid',''), g.operator['id'])
                    reset_link = request.host_url.rstrip('/') + '/reset-password#' + token
                    message = 'Одноразовая ссылка действует 30 минут. Передай её оператору лично; она позволяет задать новый пароль.'
                elif action == 'toggle':
                    uid = request.form.get('uid', '')
                    target = users.user(uid)
                    if not target or target['admin'] or target['approval'] != 'approved':
                        raise ValueError('Эту учётную запись нельзя отключить.')
                    tenant = hub.get(target)
                    with tenant.store.lock:
                        if tenant.store.busy:
                            raise ValueError('Дождись завершения текущей операции оператора.')
                        users.toggle(uid, actor=g.operator['id'])
                    if not users.user(uid)['active']:
                        hub.pool.call(tenant.store.browser, 'close')
                    message = 'Доступ и выполнение расписания оператора изменены.'
                else:
                    raise ValueError('Неизвестное действие.')
            except ValueError as exc:
                error = str(exc)
        return render_template('admin.html' if admin_view else 'account.html', active_admins=sum(bool(u['admin'] and u['active']) for u in users.users()), reset_link=reset_link, recovery=users.recovery_requests() if admin_view else [], operator=g.operator, operators=users.users() if g.operator['admin'] else [],
                               audit=users.audit() if g.operator['admin'] else [], resources=hub.pool.info(), csrf=session['csrf'], error=error, message=message)

    @app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    @app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
    def workspace(path):
        tenant = hub.get(g.operator)
        # Hide legacy token connection/API routes in multi-operator mode.
        if path == 'api/connect':
            return jsonify(error='Используй подключение VK ID.'), 410
        if request.method != 'GET' and not path.startswith('api/browser/') and path.endswith(('/browser-check', '/send', '/check')):
            if not hub.pool.available(tenant.store.browser):
                return jsonify(error='Заверши подключение своего VK. Другие операторы работают независимо.'), 409
        return Response.from_app(tenant, request.environ)

    return app
