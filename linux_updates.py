"""Manage versioned Linux installs while keeping the original data directory."""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import mac_updates
import tracker

ASSET = 'planet-helper-linux.tar.gz'
STATE = '.planet-helper-versions.json'
VERSIONS = '.planet-helper-versions'
DIRECTORY = re.compile(r'v\d+\.\d+\.\d+-[a-z0-9_]+\Z')


def entry(root, key):
    if key != '.' and (not isinstance(key, str) or not DIRECTORY.fullmatch(key)):
        raise ValueError('잘못된 설치 위치입니다.')
    path = root if key == '.' else root / VERSIONS / key
    if not (path / 'linux_app.py').is_file() or not (path / '.venv/bin/python').is_file():
        raise ValueError('실행 가능한 플래닛 도우미 설치를 찾을 수 없습니다.')
    return path


def state(root):
    try:
        active = json.loads((root / STATE).read_text())
    except FileNotFoundError:
        return {'current': '.', 'previous': None}
    return active


def launch(root, key, args=()):
    app = entry(root, key)
    env = dict(os.environ, PLANET_EXP_DATA_DIR=str(root), PLANET_HELPER_INSTALL_ROOT=str(root))
    executable = str(app / '.venv/bin/python')
    os.execve(executable, [executable, str(app / 'linux_app.py'), *args], env)


def switch(root, expected, target):
    current = state(root)
    if current['current'] != expected:
        raise ValueError('설치 버전이 이미 변경되었습니다.')
    entry(root, target)
    with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.planet-switch-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump({'current': target, 'previous': expected}, stream)
    temporary.replace(root / STATE)


class LinuxUpdateBackend:
    def __init__(self, root):
        self.root = Path(root)
        self.version = tracker.APP_VERSION

    def latest(self):
        return mac_updates.latest(self.version, ASSET)

    def can_rollback(self):
        try:
            name = state(self.root)['previous']
            return name is not None and entry(self.root, name).is_dir()
        except (OSError, ValueError, KeyError):
            return False

    def stage(self, release, rollback):
        previous = state(self.root)
        if rollback:
            name = previous['previous']
            entry(self.root, name)
            return previous['current'], name, None

        root = self.root / VERSIONS
        root.mkdir(exist_ok=True)
        version = 'v' + '.'.join(map(str, mac_updates.version(release.tag)))
        destination = Path(tempfile.mkdtemp(dir=root, prefix=f'{version}-'))
        try:
            archive = destination / ASSET
            mac_updates.download_archive(release, archive)
            with tarfile.open(archive, 'r:gz') as tar:
                tar.extractall(destination, filter='data')
            archive.unlink()
            env = dict(os.environ, PLANET_HELPER_PYTHON=sys.executable)
            subprocess.run(['/bin/bash', str(destination / 'install-linux.sh'), '--prepare'],
                           check=True, capture_output=True, timeout=600, env=env)
            entry(self.root, destination.name)
            return previous['current'], destination.name, destination
        except Exception:
            shutil.rmtree(destination)
            raise

    def restart(self, prepared):
        current, target, _ = prepared
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                 'switch', str(self.root), current, target],
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True)

    def discard(self, prepared):
        if prepared[2] is not None:
            shutil.rmtree(prepared[2])


if __name__ == '__main__':
    import fcntl

    operation, folder, *args = sys.argv[1:]
    root = Path(folder)
    with (root / 'app.lock').open('a') as lock:
        if operation == 'switch':
            expected, target = args
            for attempt in range(150):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.2)
            else:
                raise SystemExit('Timed out waiting for Planet Helper to close')
            switch(root, expected, target)
            args = []
        elif operation == 'launch':
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(0)
        else:
            raise SystemExit('Unknown Planet Helper launcher operation')
        os.set_inheritable(lock.fileno(), True)
        launch(root, state(root)['current'], args)
