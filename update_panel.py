"""Keep update checks and package preparation off the measurement/UI thread."""
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

import mac_updates
import tracker


class UpdatePanel:
    def __init__(self, controller, backend):
        self.controller = controller
        self.backend = backend
        self.version = backend.version
        self.results = queue.SimpleQueue()
        self.release = None
        self.busy = False

        self.dialog = QDialog(controller.window)
        self.dialog.setWindowTitle('플래닛 도우미 업데이트')
        layout = QVBoxLayout(self.dialog)
        self.status = QLabel(f'현재 버전 {self.version}')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        layout.addLayout(row)
        self.check_button = QPushButton('업데이트 확인')
        self.install_button = QPushButton('업데이트')
        self.rollback_button = QPushButton('이전 버전으로 롤백')
        for button in (self.check_button, self.install_button, self.rollback_button):
            row.addWidget(button)
        self.check_button.clicked.connect(self.check)
        self.install_button.clicked.connect(lambda: self.change_version(rollback=False))
        self.rollback_button.clicked.connect(lambda: self.change_version(rollback=True))

        self.timer = QTimer(controller.window)
        self.timer.timeout.connect(self.receive_result)
        self.timer.start(250)
        self.periodic = QTimer(controller.window)
        self.periodic.timeout.connect(self.check)
        self.periodic.start(30 * 60 * 1000)
        QTimer.singleShot(0, self.check)
        self.refresh_buttons()

    def show(self):
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def refresh_buttons(self):
        self.check_button.setEnabled(not self.busy)
        self.install_button.setEnabled(not self.busy and self.release is not None)
        self.rollback_button.setEnabled(not self.busy and self.backend.can_rollback())

    def start(self, operation, task):
        self.busy = True
        self.refresh_buttons()

        def work():
            try:
                self.results.put((operation, task(), None))
            except Exception as exc:
                self.results.put((operation, None, exc))

        threading.Thread(target=work, name=f'app-{operation}', daemon=True).start()

    def check(self):
        if not self.busy:
            self.status.setText('GitHub 업데이트 확인 중…')
            self.start('check', self.backend.latest)

    def change_version(self, *, rollback):
        if self.busy:
            return
        release = self.release
        if (rollback and not self.backend.can_rollback()) or (not rollback and release is None):
            return
        title = '이전 버전으로 롤백' if rollback else f'{release.tag} 업데이트'
        text = '설치가 완료되면 앱을 다시 엽니다.'
        if self.controller.running:
            text = '진행 중인 측정을 저장하고 종료한 뒤 앱을 다시 엽니다.'
        if QMessageBox.question(self.dialog, title, text) != QMessageBox.StandardButton.Yes:
            return
        self.status.setText(f'{title} 준비 중…')

        self.start('stage', lambda: self.backend.stage(release, rollback))

    def receive_result(self):
        try:
            operation, value, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if error:
            tracker.log_error('업데이트 확인' if operation == 'check' else '앱 교체 준비', error)
            reason = '업데이트를 확인할 수 없습니다.' if operation == 'check' else '앱을 교체할 수 없습니다.'
            self.status.setText(reason)
        elif operation == 'check':
            self.release, message = value
            self.status.setText(f'현재 버전 {self.version} · {message}')
            self.controller.update_button.setText('새 업데이트' if self.release else '업데이트')
        else:
            self.apply(value)
        self.refresh_buttons()

    def apply(self, prepared):
        process = None
        try:
            process = self.backend.restart(prepared)
            if self.controller.shutdown():
                return
            self.status.setText('측정 기록 저장에 실패해 앱 교체를 중단했습니다.')
        except (OSError, ValueError) as exc:
            tracker.log_error('앱 교체', exc)
            self.status.setText('앱을 교체할 수 없습니다.')
        if process is not None:
            process.terminate()
            process.wait()
        self.backend.discard(prepared)


class MacUpdateBackend:
    def __init__(self):
        self.app = Path(sys.executable).parent.parent.parent
        self.version = mac_updates.app_version(self.app)

    def can_rollback(self):
        return mac_updates.previous(self.app).is_dir()

    def latest(self):
        return mac_updates.latest(self.version)

    def stage(self, release, rollback):
        staging = mac_updates.prepare(self.app)
        try:
            stage = Path(staging.name)
            if rollback:
                candidate = stage / mac_updates.APP_NAME
                backup = mac_updates.previous(self.app)
                mac_updates.verify(backup)
                subprocess.run(['/usr/bin/ditto', str(backup), str(candidate)],
                               check=True, capture_output=True, timeout=180)
                mac_updates.verify(candidate)
            else:
                candidate = mac_updates.download(release, stage)
            return staging, candidate
        except Exception:
            staging.cleanup()
            raise

    def restart(self, prepared):
        staging, candidate = prepared
        stage = Path(staging.name)
        helper = stage / 'swap.sh'
        shutil.copyfile(tracker.RESOURCES / 'mac_update_swap.sh', helper)
        return subprocess.Popen(['/bin/sh', str(helper), str(os.getpid()), str(self.app),
                                 str(candidate), str(mac_updates.previous(self.app)),
                                 str(stage), str(tracker.BASE)],
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def discard(self, prepared):
        prepared[0].cleanup()
