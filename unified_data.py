"""One running writer and a one-time, read-only import of the EXP app's data."""
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


class DataLock:
    def __init__(self, base):
        self.base = Path(base)
        self.stream = None

    def acquire(self):
        self.base.mkdir(parents=True, exist_ok=True)
        stream = (self.base / '.app.lock').open('a+')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return False
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        self.stream = stream
        return True

    def close(self):
        if self.stream is not None:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_data(destination, legacy=None):
    """Call while holding DataLock; never replace an existing destination file.

    Pending update checkpoints are intentionally excluded: an independently named
    application must not revive another application's live measurement.
    """
    destination = Path(destination)
    legacy = Path(legacy) if legacy is not None else Path.home() / 'Library' / 'Application Support' / 'Maple Planet EXP'
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / 'legacy-import.json'
    if marker.exists():
        return json.loads(marker.read_text(encoding='utf-8'))
    report = {'source': str(legacy), 'imported': [], 'existing': [], 'sha256': {}}
    with tempfile.TemporaryDirectory(prefix='.legacy-import-', dir=destination) as folder:
        stage = Path(folder)
        for name in ('settings.json', 'records'):
            source = legacy / name
            target = destination / name
            if target.exists():
                report['existing'].append(name)
                continue
            if not source.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, stage / name)
            else:
                shutil.copy2(source, stage / name)
            copied = stage / name
            paths = sorted(copied.rglob('*')) if copied.is_dir() else [copied]
            for path in paths:
                if path.is_file():
                    relative = str(path.relative_to(stage))
                    digest = file_hash(path)
                    if digest != file_hash(legacy / relative):
                        raise OSError('기존 측정 기록이 변경되었어요. 기존 앱을 종료한 뒤 다시 실행해 주세요.')
                    report['sha256'][relative] = digest
            report['imported'].append(name)
        # Everything was copied and checked before publishing any imported file.
        for name in report['imported']:
            (stage / name).rename(destination / name)
        temporary = stage / 'legacy-import.json'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.rename(marker)
    return report
