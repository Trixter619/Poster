"""One local X display per browser. No shared desktop or network listener."""
import os
import selectors
import subprocess


class VirtualDisplay:
    def __init__(self):
        self.process = None
        self.name = None

    def start(self):
        if self.process and self.process.poll() is None:
            return self.name
        self.process = subprocess.Popen(
            ['Xvfb', '-displayfd', '1', '-screen', '0', '1360x960x24', '-nolisten', 'tcp', '-noreset'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=10):
                    raise RuntimeError('Виртуальный экран не запустился.')
                number = self.process.stdout.readline().decode('ascii').strip()
            if not number.isdecimal() or self.process.poll() is not None:
                raise RuntimeError('Виртуальный экран не запустился.')
            self.name = ':' + number
            return self.name
        except Exception:
            self.close()
            raise

    def close(self):
        process, self.process = self.process, None
        self.name = None
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
