"""Shared Windows/macOS/Linux launcher; standard library only."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from process_lock import ProcessLock

ROOT = Path(__file__).resolve().parent


def instance_id(folder):
    return hashlib.sha256(str(Path(folder).resolve()).encode()).hexdigest()


def ready(url, folder):
    try:
        with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
            info = json.load(response)
        return info == {'app': 'vk-poster', 'instance': instance_id(folder)}
    except (OSError, ValueError):
        return False


def open_panel(url):
    if os.environ.get('VK_POSTER_NO_OPEN') != '1':
        webbrowser.open(url)
    print('Панель: ' + url, flush=True)


def wait_ready(child, url, folder, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('Бот завершился при запуске. Ошибка указана выше.')
        if ready(url, folder):
            return
        time.sleep(.3)
    raise RuntimeError('Бот не открыл панель за 60 секунд.')


def main():
    folder = Path(os.environ.get('VK_POSTER_DATA', ROOT / 'data')).resolve()
    port = int(os.environ.get('VK_POSTER_PORT', '8787'))
    if not 1 <= port <= 65535:
        raise RuntimeError('VK_POSTER_PORT должен быть от 1 до 65535.')
    url = f'http://127.0.0.1:{port}'
    if ready(url, folder):
        open_panel(url)
        return
    runtime = ROOT / '.runtime'
    with ProcessLock(runtime / 'launcher.lock'):
        if ready(url, folder):
            open_panel(url)
            return
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError:
                raise RuntimeError(f'Порт {port} занят. Закрой прежний бот или задай VK_POSTER_PORT.') from None
        uv = os.environ.get('VK_POSTER_UV', str(ROOT / '.launcher' / ('uv.exe' if os.name == 'nt' else 'uv')))
        venv = runtime / 'venv'
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        env = dict(os.environ, VK_POSTER_DATA=str(folder), PLAYWRIGHT_BROWSERS_PATH=str(runtime / 'browsers'))
        def run(args):
            subprocess.run([str(x) for x in args], cwd=ROOT, env=env, check=True)
        if not python.exists():
            print('Подготовка Python…', flush=True)
            run([uv, 'venv', '--python', sys.executable, venv])
        digest = hashlib.sha256((ROOT / 'requirements.txt').read_bytes()).hexdigest()
        stamp = runtime / 'requirements.sha256'
        if not stamp.exists() or stamp.read_text() != digest:
            print('Установка зависимостей…', flush=True)
            run([uv, 'pip', 'install', '--python', python, '-r', ROOT / 'requirements.txt'])
            stamp.write_text(digest)
        # The official API runtime does not require a browser binary.
        child = subprocess.Popen([str(python), str(ROOT / 'app.py')], cwd=ROOT, env=env)
        try:
            wait_ready(child, url, folder)
            open_panel(url)
            print('Бот работает. Оставь это окно открытым. Ctrl+C — остановить.', flush=True)
            child.wait()
            if child.returncode:
                raise RuntimeError('Бот завершился с ошибкой.')
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print('Не удалось запустить VK Poster: ' + str(error), file=sys.stderr)
        sys.exit(1)
