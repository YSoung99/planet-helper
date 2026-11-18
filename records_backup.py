"""Back up hunting records and move them between computers.

A backup holds what the comparison view needs: each record's CSV and summary
JSON, plus the shared name files. Diagnostics (audit logs, captures) stay local.
Importing only adds: an existing record or name on this computer is never replaced.
"""
import json
import re
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import tracker

RECORD = re.compile(r'\d{8}-\d{6}-\d+\.(csv|json)\Z')
SHARED = ('memos.json', 'character-names.json', 'segment-tags.json', 'map-names.json')
BACKUP_FOLDER = 'records-backups'
MAX_ENTRY = 64*1024*1024


def records_folder(base=None):
    return Path(base or tracker.BASE)/'records'


def backup_names(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir()
                  if path.is_file() and (RECORD.fullmatch(path.name) or path.name in SHARED))


def export_zip(destination, base=None):
    """Write a backup zip atomically; returns the number of records in it."""
    folder = records_folder(base)
    names = backup_names(folder)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.backup-', suffix='.zip')
    temporary = Path(temporary)
    try:
        with open(handle, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('planet-helper-backup.json', json.dumps(
                {'format': 1, 'app_version': tracker.APP_VERSION, 'created': time.time()}))
            for name in names:
                archive.write(folder/name, f'records/{name}')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sum(1 for name in names if name.endswith('.csv'))


def _merge_maps(local, incoming, depth):
    """Add keys missing locally. depth=2 merges one level deeper (session -> {minute: tag})."""
    if not isinstance(incoming, dict):
        return local, 0
    added = 0
    for key, value in incoming.items():
        if key not in local:
            local[key] = value; added += 1
        elif depth > 1 and isinstance(local[key], dict) and isinstance(value, dict):
            local[key], more = _merge_maps(local[key], value, depth-1)
            added += more
    return local, added


def _write_json(path, data):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def import_zip(source, base=None):
    """Merge a backup into this computer. Returns (records added, records skipped, names added)."""
    folder = records_folder(base)
    folder.mkdir(parents=True, exist_ok=True)
    added = skipped = names = 0
    with zipfile.ZipFile(source) as archive:
        if 'planet-helper-backup.json' not in archive.namelist():
            raise ValueError('플래닛 도우미 백업 파일이 아니에요.')
        entries = {}
        for info in archive.infolist():
            name = info.filename.removeprefix('records/')
            # Only plain file names we know: no folders, no paths outside records/.
            if info.is_dir() or '/' in name or not (RECORD.fullmatch(name) or name in SHARED):
                continue
            if info.file_size > MAX_ENTRY:
                raise ValueError(f'{name} 파일이 너무 커요.')
            entries[name] = info
        sessions = {name.rsplit('.', 1)[0] for name in entries if RECORD.fullmatch(name)}
        for session in sorted(sessions):
            if (folder/f'{session}.csv').exists() or f'{session}.csv' not in entries:
                skipped += 1; continue
            for suffix in ('.json', '.csv'):  # CSV last: a record counts once its CSV exists
                info = entries.get(session+suffix)
                if info is None: continue
                temporary = folder/f'.import-{session}{suffix}'
                temporary.write_bytes(archive.read(info))
                temporary.replace(folder/(session+suffix))
            added += 1
        for shared in SHARED:
            if shared not in entries: continue
            try:
                incoming = json.loads(archive.read(entries[shared]).decode('utf-8'))
            except (ValueError, UnicodeError):
                continue
            path = folder/shared
            try:
                local = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                local = {}
            if not isinstance(local, dict): local = {}
            depth = 2 if shared in ('segment-tags.json', 'map-names.json') else 1
            merged, count = _merge_maps(local, incoming, depth)
            if count:
                _write_json(path, merged); names += count
    return added, skipped, names


def auto_backup(base=None, keep=14, today=None):
    """Once per day, keep a dated backup next to the records. Returns the new file or None."""
    folder = Path(base or tracker.BASE)/BACKUP_FOLDER
    if not backup_names(records_folder(base)):
        return None
    stamp = (today or datetime.now()).strftime('%Y%m%d')
    target = folder/f'records-{stamp}.zip'
    if target.exists():
        return None
    export_zip(target, base)
    backups = sorted(folder.glob('records-[0-9]*.zip'))
    for old in backups[:max(0, len(backups)-keep)]:
        old.unlink(missing_ok=True)
    return target
