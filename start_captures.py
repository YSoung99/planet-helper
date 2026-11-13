"""Save immutable game frames at measurement milestones off the UI thread."""
import json
import queue
import threading
import time
from pathlib import Path


class StartCaptures:
    STAGES = ('first-frame', 'baseline', 'hunt-start')
    KEEP_SESSIONS = 10

    def __init__(self, folder, keep=KEEP_SESSIONS):
        self.folder = Path(folder)
        self.keep = keep
        self.jobs = queue.Queue(maxsize=9)
        self.results = queue.Queue()
        self.claimed = set()
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='start-capture-save', daemon=True)
        self.thread.start()

    def submit(self, session_id, stage, image, roi_image, metadata):
        if stage not in self.STAGES or not session_id or any(c not in '0123456789-' for c in session_id):
            raise ValueError('Invalid start capture identifier')
        key = (session_id, stage)
        if self.closed or key in self.claimed:
            return False
        if (self.folder/session_id/(stage+'.json')).exists():
            self.claimed.add(key)
            return False
        # The capture worker never mutates these frames after delivering an event.
        job = (key, image, roi_image, dict(metadata))
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            self.results.put((key, '시작 화면 저장 대기열이 가득 참'))
            return False
        self.claimed.add(key)
        return True

    @staticmethod
    def _atomic_image(image, destination):
        temporary = destination.with_suffix('.png.tmp')
        try:
            image.save(temporary, format='PNG')
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _write(self, key, image, roi_image, metadata):
        session_id, stage = key
        directory = self.folder/session_id
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory/(stage+'.json')
        if destination.exists():
            return
        self._atomic_image(image, directory/(stage+'.png'))
        self._atomic_image(roi_image, directory/(stage+'-exp.png'))
        metadata.update(session_id=session_id, stage=stage,
                        image=stage+'.png', exp_image=stage+'-exp.png',
                        image_pixels=list(image.size), exp_pixels=list(roi_image.size))
        temporary = destination.with_suffix('.json.tmp')
        try:
            temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def prune(self):
        """Keep only the newest sessions' start frames; touch nothing but our own files."""
        if not self.keep or not self.folder.is_dir():
            return 0
        sessions = sorted(path for path in self.folder.iterdir() if path.is_dir()
                          and path.name and all(c in '0123456789-' for c in path.name))
        removed = 0
        own = {stage+suffix for stage in self.STAGES
               for suffix in ('.png', '-exp.png', '.json', '.png.tmp', '-exp.png.tmp', '.json.tmp')}
        for directory in sessions[:-self.keep]:
            for file in directory.iterdir():
                if file.name in own and file.is_file():
                    file.unlink(missing_ok=True)
            try:
                directory.rmdir()
                removed += 1
            except OSError:
                pass  # unexpected files stay untouched
        return removed

    def _run(self):
        try:
            self.prune()
        except OSError as exc:
            self.results.put((('', 'prune'), f'오래된 시작 화면 정리 실패: {exc}'))
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                key, image, roi_image, metadata = job
                error = None
                for attempt in range(2):
                    try:
                        self._write(key, image, roi_image, metadata)
                        error = None
                        break
                    except Exception as exc:
                        error = str(exc)
                        if attempt == 0:
                            time.sleep(.1)
                self.results.put((key, error))
            finally:
                self.jobs.task_done()

    def flush(self):
        self.jobs.join()

    def close(self):
        if not self.closed:
            self.closed = True
            self.jobs.put(None)
            self.thread.join()
