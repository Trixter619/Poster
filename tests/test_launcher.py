import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import launcher
from process_lock import ProcessLock
from app import create_app


class LauncherTests(unittest.TestCase):
    def test_second_process_cannot_lock_and_crash_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'process.lock'
            code = 'from process_lock import ProcessLock; import sys; ProcessLock(sys.argv[1]).__enter__()'
            with ProcessLock(path):
                result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertEqual(subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True).returncode, 0)
            with ProcessLock(path):
                pass

    def test_windows_lock_and_unlock_same_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lock'
            lock = ProcessLock(path)
            api = Mock(LK_NBLCK=2, LK_UNLCK=0)
            with patch('process_lock.os.name', 'nt'), patch.dict(sys.modules, msvcrt=api):
                with lock:
                    self.assertEqual(lock.file.tell(), 0)
                self.assertEqual([c.args[1:] for c in api.locking.call_args_list], [(2, 1), (0, 1)])

    def test_health_identifies_data_folder_without_disclosing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = create_app(Path(tmp)).test_client().get('/api/health')
            self.assertEqual(response.json, {'app':'vk-poster', 'instance':launcher.instance_id(tmp)})
            self.assertNotIn(tmp, response.get_data(as_text=True))

    def test_duplicate_launch_only_opens_panel(self):
        with patch('launcher.ready', return_value=True), patch('launcher.open_panel') as opened, patch('launcher.subprocess.Popen') as spawn:
            launcher.main()
            opened.assert_called_once()
            spawn.assert_not_called()

    def test_unrelated_service_is_not_ready(self):
        response = io.BytesIO(json.dumps({'app':'other'}).encode())
        with patch('launcher.urllib.request.urlopen', return_value=response):
            self.assertFalse(launcher.ready('http://127.0.0.1:8787', '/tmp/example'))

    def test_failed_child_does_not_open_browser(self):
        child = Mock()
        child.poll.return_value = 1
        with self.assertRaises(RuntimeError):
            launcher.wait_ready(child, 'http://127.0.0.1:8787', '/tmp/example')
