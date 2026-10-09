"""Native Chrome and an operator-local desktop. All listeners stay on loopback."""
import os
from pathlib import Path
import socket
import subprocess
import time


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class DesktopBrowser:
    def __init__(self):
        self.chrome = self.wm = self.vnc = self.bridge = None
        self.screen_port = None
        self.screen_until = 0
        self.env = None
        self.endpoint = None

    def start(self, profile, display, initial_url="about:blank"):
        self.env = dict(os.environ, DISPLAY=display)
        self.wm = subprocess.Popen(['openbox'], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        marker = Path(profile) / 'DevToolsActivePort'
        # This file contains only the local debugger endpoint, not session data.
        marker.unlink(missing_ok=True)
        executable = os.environ.get('VK_NATIVE_CHROME', '/opt/vk-poster/.runtime/chrome-stable/opt/google/chrome/chrome')
        self.chrome = subprocess.Popen([executable, '--user-data-dir=' + str(profile),
            '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', '--start-maximized', initial_url],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.chrome.poll() is not None:
                raise RuntimeError('Chrome не запустился.')
            if marker.exists():
                with marker.open() as f:
                    number = f.readline().strip()
                if number.isdecimal() and 0 < int(number) < 65536:
                    self.endpoint = 'http://127.0.0.1:' + number
                    return self.endpoint
            time.sleep(.1)
        raise RuntimeError('Chrome не ответил вовремя.')

    def open_screen(self):
        if self.bridge and self.bridge.poll() is None and self.vnc and self.vnc.poll() is None:
            self.screen_until = time.monotonic() + 30
            return
        self.close_screen()
        vnc_port, ws_port = port(), port()
        self.vnc = subprocess.Popen(['x11vnc', '-display', self.env['DISPLAY'], '-localhost',
            '-rfbport', str(vnc_port), '-no6', '-nopw', '-forever', '-shared', '-noxdamage', '-quiet'],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self._wait_port(vnc_port, self.vnc)
            self.bridge = subprocess.Popen(['websockify', '127.0.0.1:' + str(ws_port),
                '127.0.0.1:' + str(vnc_port)], env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1'),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._wait_port(ws_port, self.bridge)
            self.screen_port = ws_port
            self.screen_until = time.monotonic() + 30
        except Exception:
            self.close_screen()
            raise

    @staticmethod
    def _wait_port(number, process):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Сервис экрана не запустился.')
            try:
                with socket.create_connection(('127.0.0.1', number), timeout=.2):
                    return
            except OSError:
                time.sleep(.1)
        raise RuntimeError('Экран не ответил вовремя.')

    def expire(self):
        if self.screen_port and time.monotonic() >= self.screen_until:
            self.close_screen()

    def close_screen(self):
        self.screen_port = None
        self.screen_until = 0
        stop(self.bridge)
        stop(self.vnc)
        self.bridge = self.vnc = None

    def close(self):
        self.close_screen()
        stop(self.chrome)
        stop(self.wm)
        self.chrome = self.wm = None
