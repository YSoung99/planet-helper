"""Share record backups between computers through a cloud folder, using rclone.

Each computer uploads only its own file (records-<computer>.zip) and merges the
others' files with records_backup.import_zip, which never replaces local data.
rclone keeps the cloud login; this app never sees a password or token.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import records_backup
import tracker

REMOTE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_. -]*:[^\n\r\0]*\Z')
STATE = 'cloud-sync.json'
TIMEOUT = 180


def rclone():
    found = shutil.which('rclone')
    if found:
        return found
    # Apps started from the macOS Finder do not get Homebrew on PATH.
    for candidate in ('/opt/homebrew/bin/rclone', '/usr/local/bin/rclone', '/usr/bin/rclone'):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def valid_remote(remote):
    return isinstance(remote, str) and bool(REMOTE.fullmatch(remote)) and '..' not in remote.split(':', 1)[1]


def computer_name():
    name = re.sub(r'[^A-Za-z0-9_-]+', '-', socket.gethostname().split('.')[0]).strip('-')
    return (name or 'computer')[:40]


def _run(*args, timeout=TIMEOUT):
    binary = rclone()
    if binary is None:
        raise RuntimeError('rclone이 설치돼 있지 않아요.')
    result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(message[-1] if message else f'rclone 오류 {result.returncode}')
    return result.stdout


def remotes():
    """Configured rclone remotes, e.g. ['gdrive:']."""
    if rclone() is None:
        return []
    try:
        return [line.strip() for line in _run('listremotes', timeout=20).splitlines() if line.strip()]
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return []


def _join(remote, name):
    return remote + name if remote.endswith((':', '/')) else f'{remote}/{name}'


def push(remote, base=None, name=None):
    """Upload this computer's backup. Returns the uploaded file name."""
    name = name or f'records-{computer_name()}.zip'
    with tempfile.TemporaryDirectory(prefix='planet-cloud-') as folder:
        archive = Path(folder)/name
        records_backup.export_zip(archive, base)
        _run('mkdir', remote)
        _run('copyto', str(archive), _join(remote, name))
    return name


def pull(remote, base=None, own=None):
    """Merge every other computer's backup. Returns (files, records added, names added)."""
    own = own or f'records-{computer_name()}.zip'
    _run('mkdir', remote)  # the first computer to sync finds no folder yet
    try:
        listing = json.loads(_run('lsjson', '--files-only', remote) or '[]')
    except ValueError:
        raise RuntimeError('클라우드 폴더 목록을 읽지 못했어요.')
    files = added = names = 0
    for entry in listing:
        name = entry.get('Name') if isinstance(entry, dict) else None
        if not (isinstance(name, str) and re.fullmatch(r'records-[A-Za-z0-9_-]+\.zip', name)) or name == own:
            continue
        if entry.get('Size', 0) > 512*1024*1024:
            continue
        with tempfile.TemporaryDirectory(prefix='planet-cloud-') as folder:
            local = Path(folder)/name
            _run('copyto', _join(remote, name), str(local))
            new, _, merged = records_backup.import_zip(local, base)
        files += 1; added += new; names += merged
    return files, added, names


def sync(remote, base=None):
    """Pull first so the upload already contains what other computers added."""
    if not valid_remote(remote):
        raise ValueError('클라우드 위치가 올바르지 않아요.')
    files, added, names = pull(remote, base)
    uploaded = push(remote, base)
    result = dict(time=time.time(), ok=True, files=files, added=added, names=names, uploaded=uploaded)
    save_state(result, base)
    return result


def state_path(base=None):
    return Path(base or tracker.BASE)/STATE


def save_state(result, base=None):
    path = state_path(base)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def load_state(base=None):
    try:
        data = json.loads(state_path(base).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(state):
    if not state.get('time'):
        return '아직 동기화하지 않았어요.'
    when = time.strftime('%m/%d %H:%M', time.localtime(state['time']))
    if not state.get('ok'):
        return f"{when} 동기화 실패 · {state.get('error', '')}"
    return (f"{when} 동기화 · 다른 컴퓨터 백업 {state['files']}개 · "
            f"새 기록 {state['added']}개 · 이름 {state['names']}개 합침")
