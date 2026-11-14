"""Qt controller shared by the Linux and macOS apps.

Platform modules (linux_app.py, mac_app.py) subclass SharedController and only
override the hooks marked "Platform hook" below; every other change made here
applies to both apps.
"""
import argparse
import csv
import ctypes
import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QTimer, QObject, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QFileDialog, QMessageBox, QMenu, QMenuBar, QDialog, QComboBox)
import character_hud
import hunt_activity
import map_name
import tracker
from recent_exp import RecentExp


def level_mismatch(details, level, total):
    """Explain rejected OCR without relaxing the accounting checks."""
    from temporal_ocr import consistent_value
    frames = details.get('temporal', {}).get('frames', [])
    readings = [f['value'] for f in frames if f.get('value') is not None and f.get('score', 0) >= .85]
    if total is None or len(readings) < 2 or not all(not consistent_value(v,total,level) for v in readings):
        return None
    matches = [candidate for candidate, denominator in tracker.EXP_TABLE.items()
               if all(consistent_value(v, denominator, candidate) for v in readings)]
    hint = f' · 판독 비율은 Lv.{matches[0]}에 일치' if len(matches) == 1 else ''
    return f'레벨 설정 확인 · 현재 Lv.{level}의 경험치통과 불일치'+hint


class Value:
    def __init__(self, value=''): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value


class LevelValue:
    def __init__(self, widget): self.widget = widget
    def get(self): return self.widget.value()
    def set(self, value):
        previous = self.widget.blockSignals(True)
        self.widget.setValue(value)
        self.widget.blockSignals(previous)


def short_exp(value):
    """Korean units: 1.23억 / 4,567만."""
    return f'{value/1e8:.2f}억' if value >= 1e8 else f'{value/1e4:,.0f}만' if value >= 1e4 else f'{value:,.0f}'


def level_eta_text(rate, session):
    """Hourly EXP and time to the next level from the cumulative per-minute average."""
    level = f'Lv.{session.level} ' if session.level else ''
    if rate['status'] != 'ready' or not rate['cumulative'] or not session.total or not session.last:
        return f'{level}레벨업 예상 · 평균 집계 후 표시'
    remaining = max(0, session.total-session.last[0])
    minutes = math.ceil(remaining/rate['cumulative'])
    eta = f'{minutes//60}시간 {minutes%60}분' if minutes >= 60 else f'{minutes}분'
    ups = f' · 레벨업 {len(session.level_ups)}회' if session.level_ups else ''
    return (f'{level}시간당 {short_exp(rate["cumulative"]*60)} · 레벨업까지 {eta} '
            f'({remaining/session.total*100:.1f}% 남음){ups}')


class TrackerWindow(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        self.drag_origin = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(21, 27, 36, round(self.controller.window_opacity*255)))
        painter.setPen(QPen(QColor('#35414d'), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.fillRect(0, 0, self.width(), 3, QColor('#a8edbb'))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 44:
            if sys.platform == 'darwin':
                self.drag_origin = event.globalPosition().toPoint()-self.pos()
            else:
                self.windowHandle().startSystemMove()
                self.drag_origin = None

    def mouseMoveEvent(self, event):
        if self.drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint()-self.drag_origin)

    def mouseReleaseEvent(self, event):
        if self.drag_origin is not None:
            self.drag_origin = None
            self.controller.save_config()

    def closeEvent(self, event):
        if self.controller.shutdown(): event.accept()
        else: event.ignore()


class Selection(QWidget):
    def __init__(self, controller, monitor, index):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        self.monitor = monitor
        self.start = self.end = None
        self.setWindowTitle(f'Planet EXP Region {index}')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(monitor['left'], monitor['top'], monitor['width'], monitor['height'])
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.cancel_shortcut = QShortcut(QKeySequence('Escape'), self)
        self.cancel_shortcut.activated.connect(controller.cancel_selection)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 37, 54, 75))
        painter.setPen(QColor('#ffda70'))
        painter.drawText(20, 35, '경험치 숫자와 [퍼센트]만 드래그 · ESC 취소')
        if self.start is not None:
            painter.setPen(QPen(QColor('#ff5555'), 2))
            painter.drawRect(QRect(self.start, self.end).normalized())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self.end = event.position().toPoint()
            self.update()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start is None: return
        self.end = event.position().toPoint()
        rect = QRect(self.start, self.end).normalized().intersected(self.rect())
        if rect.width() < 40 or rect.height() < 8: return
        point = self.mapToGlobal(rect.topLeft())
        self.controller.region = dict(left=point.x(), top=point.y(), width=rect.width(), height=rect.height())
        self.controller.save_config()
        self.controller.reset()
        self.controller.status.set('영역 저장됨 · 시작 버튼 클릭')
        self.controller.cancel_selection()


class SnapshotSignals(QObject):
    ready = Signal(int,object)
    failed = Signal(int,str)


class WindowImage(QWidget):
    def __init__(self, dialog, image):
        super().__init__(dialog)
        self.dialog = dialog
        self.start = self.end = None
        rgb = image.convert('RGB')
        self.pixmap = QPixmap.fromImage(QImage(rgb.tobytes(),rgb.width,rgb.height,rgb.width*3,QImage.Format.Format_RGB888).copy())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName('게임 경험치 영역 선택 이미지')

    def paintEvent(self,event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(),self.pixmap,self.pixmap.rect())
        if self.start is not None:
            painter.setPen(QPen(QColor('#ff5555'),2))
            painter.drawRect(QRect(self.start,self.end).normalized())

    def mousePressEvent(self,event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self.end = event.position().toPoint(); self.update()

    def mouseMoveEvent(self,event):
        if self.start is not None:
            self.end = event.position().toPoint(); self.update()

    def mouseReleaseEvent(self,event):
        if self.start is None: return
        self.end = event.position().toPoint()
        rect = QRect(self.start,self.end).normalized().intersected(self.rect())
        points = self.dialog.image.info['content_points']
        if rect.width()/self.width()*points[0] < 40 or rect.height()/self.height()*points[1] < 8:
            self.dialog.instruction.setText('선택이 너무 작아요 · 경험치 숫자와 [퍼센트]를 함께 선택해 주세요')
            return
        roi = dict(left=rect.x()/self.width(),top=rect.y()/self.height(),
                   width=rect.width()/self.width(),height=rect.height()/self.height(),
                   reference_aspect=points[0]/points[1])
        self.dialog.controller.apply_window_region(roi,self.dialog.image)


class WindowSelection(QDialog):
    def __init__(self,controller,image):
        super().__init__(controller.window,Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.controller,self.image = controller,image
        self.setWindowTitle('게임 경험치 영역 지정')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        self.instruction = QLabel('게임 창 이미지에서 경험치 숫자와 [퍼센트]만 드래그 · ESC 취소')
        layout.addWidget(self.instruction)
        self.image_view = WindowImage(self,image)
        available = controller.application.primaryScreen().availableGeometry()
        factor = min((available.width()-80)/image.width,(available.height()-140)/image.height,1)
        self.image_view.setFixedSize(round(image.width*factor),round(image.height*factor))
        layout.addWidget(self.image_view)
        self.setFixedSize(self.sizeHint())
        self.move(available.center()-self.rect().center())


class SharedController(tracker.MeasurementCore):
    # ---- Platform hooks (override in linux_app.py / mac_app.py) ----
    APP_TITLE = '플래닛 도우미'
    NATIVE_MENU_BAR = False
    CAPTURE_MODULE = 'linux_capture'
    CAPTURE_OPTIONS = {}
    CAPTURE_BACKENDS = ()
    REPEAT_NEEDS_PERMISSION = False

    def native_overlay(self, widget, level=3):
        """Keep a window above others / on all Spaces where the platform needs it."""

    def repeat_bridge_class(self):
        from key_repeat import KeyRepeatBridge
        return KeyRepeatBridge

    def apply_pin(self, enabled):
        """Apply always-on-top; return the state that actually took effect."""
        position = self.window.pos()
        visible = self.window.isVisible()
        self.window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        if visible:
            self.window.show()
            self.native_overlay(self.window, 3 if enabled else 0)
        self.window.move(position)
        return enabled

    def add_repeat_permission_widgets(self, layout):
        """Add platform permission help to the repeat dialog; return a cleanup callable."""
        return lambda: None

    def extra_menu_actions(self, menu):
        pass

    def open_path(self, path):
        subprocess.Popen(['xdg-open', str(path)])

    def check_permission(self):
        return True

    def has_background_capture_permission(self):
        return True

    def capture_module(self):
        import importlib
        return importlib.import_module(self.CAPTURE_MODULE)

    # ---- Shared behaviour ----
    def __init__(self, application, start_worker=True, restore=True, start_repeat=None):
        self.application = application
        self.running = False
        self.generation = 0
        self.events = queue.Queue()
        self.engine = None
        self.last_update = None
        self.scan_seconds = 0
        self.target_seconds = 1.0
        self.preview_frame = self.preview_stamp = None
        self.save_warning = self.audit_warning = ''
        self.worker_ready = False
        self.stop = threading.Event()
        self.overlays = []
        self.selection_generation = 0
        self.capture_clients = threading.local()
        self.snapshot_signals = SnapshotSignals()
        self.snapshot_signals.ready.connect(self.present_window_selection)
        self.snapshot_signals.failed.connect(self.selection_failed)
        self.status = Value('측정 시작으로 레벨·경험치 위치를 찾으세요')
        self.auto_rescan = threading.Event()
        self.auto_read_failures = 0
        from hud_information import HudInformation
        self.hud_information = HudInformation()
        self.info_dialog = None
        self.value = Value('경험치 대기 중')
        self.metrics = Value()
        tracker.BASE.mkdir(parents=True, exist_ok=True)
        if restore:
            try: tracker.prune_diagnostics()
            except OSError as exc: tracker.log_error('진단 기록 정리', exc)
        from start_captures import StartCaptures
        self.start_captures = StartCaptures(tracker.BASE/'records'/'start-captures')
        self.capture_failures = {}
        self.map_results = queue.Queue()
        self.jev_status_session = None
        self.jev_revision = 0
        self.hunt_activity = hunt_activity.Activity()
        self.hunt_activity_results = queue.SimpleQueue()
        self.hunt_activity_lock = threading.Lock()
        self.hunt_activity_cache = {}
        self.character_results = queue.SimpleQueue()
        self.character_candidate = (None, None)
        self.character_identity = (None, None)
        self.character_source = None
        self.character_recheck_at = 0.0
        config, warning = tracker.load_settings(tracker.BASE/'settings.json') if restore else ({}, None)
        self.auto_hud = config.get('auto_hud', True)
        self.recent_recording = config.get('recent_recording', True)
        self.burning = config.get('burning', False)
        self.jev_enabled = config.get('jev_enabled', False)
        self.recent_permission = start_worker and self.has_background_capture_permission()
        self.recent_exp = RecentExp()
        self.recent_start_source = None
        if not self.auto_hud:
            self.status.set('수동 인식 · 영역과 레벨을 확인해 주세요')
        self.region = config.get('region')
        self.window_region = config.get('window_region')
        self.session = tracker.Session(level=config.get('level', 169))
        self.window = TrackerWindow(self)
        from display_panel import build
        build(self, config, application, LevelValue, repeat_bridge=self.repeat_bridge_class())
        self.source_label.setText('MapleStory Worlds · 게임 창 캡처')
        self.window.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.window.customContextMenuRequested.connect(self.context_menu)
        self.quit_shortcut = QShortcut(QKeySequence.StandardKey.Quit, self.window)
        self.quit_shortcut.activated.connect(self.close)
        self.menu_bar = QMenuBar()
        self.menu_bar.setNativeMenuBar(self.NATIVE_MENU_BAR)
        app_menu = self.menu_bar.addMenu(self.APP_TITLE)
        app_menu.addAction('정보', self.show_information)
        quit_action = QAction('종료', self.menu_bar)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.close)
        app_menu.addAction(quit_action)
        resume_repeat = False
        if restore:
            self.restore_latest_record()
            checkpoint = tracker.BASE/'pending-update.json'
            if checkpoint.exists():
                if self.resume_handoff(checkpoint):
                    try:
                        resume_repeat = json.loads(checkpoint.read_text(encoding='utf-8')).get('repeat_enabled') is True
                    except (OSError, ValueError) as exc:
                        tracker.log_error('업데이트 연타 상태 확인', exc)
                checkpoint.unlink(missing_ok=True)
        if warning: self.status.set(warning)
        self.timer = QTimer(); self.timer.timeout.connect(self.poll); self.timer.start(200)
        repeat_requested = start_worker if start_repeat is None else start_repeat
        if repeat_requested:
            QTimer.singleShot(0, self.key_repeat.start)
            if resume_repeat:
                QTimer.singleShot(0, lambda: self.key_repeat.set_enabled(True))
        if start_worker: threading.Thread(target=self.worker, daemon=True).start()

    def show_information(self):
        if self.info_dialog is not None:
            try:
                if self.info_dialog.isVisible():
                    self.info_dialog.raise_()
                    self.info_dialog.activateWindow()
                    return
            except RuntimeError:
                pass
        from hud_information import build_dialog
        self.info_dialog = build_dialog(self)
        self.native_overlay(self.info_dialog, 4)

    def toggle_auto_hud(self, enabled):
        if not self.finish_measurement('인식 방식 변경'):
            previous = self.auto_checkbox.blockSignals(True)
            self.auto_checkbox.setChecked(self.auto_hud)
            self.auto_checkbox.blockSignals(previous)
            return
        self.auto_hud = bool(enabled)
        self.level_picker.setEnabled(not self.auto_hud)
        self.auto_rescan.set()
        self.save_config()
        self.status.set('자동 인식 준비 · 측정 시작을 눌러 주세요' if enabled else '수동 인식 · 영역과 레벨을 확인해 주세요')

    def opacity_settings(self):
        from PySide6.QtWidgets import QInputDialog
        value, ok = QInputDialog.getInt(self.window, '배경 투명도', '배경 불투명도 (%)', round(self.window_opacity*100), 20, 100)
        if ok:
            self.window_opacity = value/100
            self.window.update()
            self.save_config()

    def toggle_display(self):
        self.compact = not self.compact
        from display_panel import resize
        resize(self)
        self.save_config()

    def toggle_pin(self, checked):
        enabled = self.apply_pin(bool(checked))
        self.always_on_top = enabled
        previous = self.pin_button.blockSignals(True)
        self.pin_button.setChecked(enabled)
        self.pin_button.blockSignals(previous)
        self.pin_button.setText('맨 위 켬' if self.always_on_top else '맨 위 끔')
        self.save_config()

    def update_rate_display(self, now):
        session = self.session
        gap = int(session.gap_seconds(now))
        last = session.samples[-1] if session.samples else None
        age = (session.ended if session.ended is not None else now) - last[0] if last else 0
        inputs = (session.session_id, last, session.ended, age > 6, session.level, gap, session.blocked)
        if inputs == self.rate_inputs_key:
            return
        self.rate_inputs_key = inputs
        rate = session.rate_estimate(now)
        key = (session.session_id, int(rate['duration']//10), rate['status'], rate['recent_status'],
               session.ended is not None, rate['age'] > 6, session.level, gap, rate['gap_gain'], session.blocked)
        if key == self.rate_display_key:
            return
        self.rate_display_key = key
        if rate['status'] == 'ready':
            average = f"{rate['cumulative']:,.0f}"
            if rate['recent'] is not None:
                recent = f"{rate['recent']:,.0f}"
            else:
                recent = '관측 공백' if rate['recent_status'] == 'gap' else '관측 부족'
            observed = max(0, int(rate['asof']-self.session.started))
            info = f'평균 {observed//60}:{observed%60:02d} 관측 기준 · 10초마다 갱신'
            if self.session.ended is not None:
                info = f'마지막 확인 {observed//60}:{observed%60:02d} 기준 · 정지 기록'
                if rate['age'] > 6:
                    info += f" · 종료 전 {rate['age']:.0f}초 관측 공백"
            if rate['recent_seconds'] < 180:
                info += f" · 최근 {rate['recent_seconds']:.0f}초 구간"
        elif rate['status'] == 'warming':
            average = recent = '집계 중'
            info = f"평균 첫 집계까지 {max(0,math.ceil(60-rate['duration']))}초 · 10초마다 갱신"
            if self.session.ended is not None:
                average = recent = '관측 부족'
                info = '정지 기록 · 평균 계산에 필요한 60초 관측 미확보'
        elif rate['status'] == 'stale':
            average = recent = '인식 지연'
            info = '새 관측 대기 · 평균 계산 보류'
        else:
            average = recent = '—'
            info = '평균은 60초 관측 후 표시 · 10초마다 갱신'
        if gap:
            info += f" · 공백 {gap//60}:{gap%60:02d} / {rate['gap_gain']:,.0f} EXP 제외"
        if self.session.blocked:
            info = '감소 충돌로 정지 · 총량/평균 확인 필요 · '+info
        self.minute_label.setText(average); self.six_label.setText(recent)
        self.compact_average.setText(average); self.compact_six_average.setText(f"{rate['cumulative']*6:,.0f}" if rate['status']=='ready' else average)
        self.rate_info.setText(info)
        self.compact_average.setToolTip(info)
        self.compact_six_average.setToolTip(info)
        self.level_eta.setText(level_eta_text(rate, self.session))

    def update_repeat(self, state):
        enabled = bool(state.get('enabled'))
        self.repeat_button.setText('연타 끄기' if enabled else '연타 켜기')
        ready = (state.get('accessibility') and state.get('inputMonitoring')
                 and not state.get('error') and not state.get('legacyRunning'))
        self.repeat_button.setEnabled(not self.key_repeat.closing and
                                      (not self.REPEAT_NEEDS_PERMISSION or bool(enabled or ready)))
        message = state.get('status') or ('연타 켜짐' if enabled else '연타 꺼짐')
        if not state.get('accessibility') or not state.get('inputMonitoring'):
            missing = []
            if not state.get('accessibility'): missing.append(state.get('postPermissionName', '손쉬운 사용'))
            if not state.get('inputMonitoring'): missing.append('입력 모니터링')
            message = '권한 필요 · '+' / '.join(missing)+' · 설정'
        if state.get('legacyRunning'):
            message = '기존 키 반복 앱을 종료한 뒤 연타를 켜 주세요'
        if state.get('error'):
            message = state['error']
        self.repeat_status.setText(message)
        self.repeat_status.setAccessibleDescription(message)
        self.repeat_status.setStyleSheet('color: '+('#a8edbb' if enabled else '#e7b86e' if state.get('error') or state.get('legacyRunning') else '#98a7b7')+';')

    def toggle_repeat(self):
        self.key_repeat.set_enabled(not self.key_repeat.state.get('enabled', False))

    def repeat_settings(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle('연타 설정과 권한')
        dialog.setStyleSheet('QDialog { background: #151b24; } QComboBox { background: #25303d; padding: 4px; }')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('키를 누르는 동안 게임 창에서만 반복해요.'))
        layout.addWidget(QLabel('설정을 저장하면 연타가 꺼져요.'))
        grid = QGridLayout()
        settings = self.key_repeat.settings
        pickers = {}
        for row, (key, label, low, high) in enumerate((
                ('firstDelayMs', '첫 대기', 100, 2000),
                ('minIntervalMs', '반복 최소 간격', 50, 1000),
                ('maxIntervalMs', '반복 최대 간격', 50, 1000))):
            picker = QSpinBox(); picker.setRange(low, high)
            picker.setSuffix(' ms'); picker.setValue(settings[key]); picker.setAccessibleName(label)
            grid.addWidget(QLabel(label), row, 0); grid.addWidget(picker, row, 1)
            pickers[key] = picker
        trigger = QComboBox()
        trigger.addItem('A 키', 0); trigger.addItem('Z 키 → A 입력', 6)
        trigger.setCurrentIndex(1 if settings['attackTriggerKeyCode'] == 6 else 0)
        grid.addWidget(QLabel('공격 키'), 3, 0); grid.addWidget(trigger, 3, 1)
        layout.addLayout(grid)
        cleanup = self.add_repeat_permission_widgets(layout)
        failure = QLabel(self.key_repeat.settings_error); failure.setWordWrap(True)
        layout.addWidget(failure)
        actions = QHBoxLayout()
        save = QPushButton('저장'); cancel = QPushButton('닫기')
        def save_settings():
            selected = {key:picker.value() for key,picker in pickers.items()}
            selected['attackTriggerKeyCode'] = trigger.currentData()
            try:
                self.key_repeat.configure(selected)
            except (OSError, ValueError, RuntimeError) as exc:
                failure.setText(str(exc)); return
            dialog.accept()
        save.clicked.connect(save_settings); cancel.clicked.connect(dialog.reject)
        actions.addWidget(save); actions.addWidget(cancel); layout.addLayout(actions)
        try:
            dialog.exec()
        finally:
            cleanup()

    @staticmethod
    def label(text, name='muted'):
        label = QLabel(text); label.setObjectName(name)
        return label

    @staticmethod
    def line():
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet('color: #2b3542;'); return line

    def show(self):
        self.window.show()
        self.native_overlay(self.window, 3 if self.always_on_top else 0)
        self.window.raise_()
        self.window.activateWindow()

    def context_menu(self, point):
        menu = QMenu(self.window)
        menu.addAction('정보', self.show_information)
        menu.addAction('사냥 기록 비교', self.show_history)
        collect = menu.addAction('대기 중 최근 경험치 모으기')
        collect.setCheckable(True)
        collect.setChecked(self.recent_recording)
        collect.toggled.connect(self.set_recent_recording)
        burning = menu.addAction('플래닛 버닝 적용')
        burning.setCheckable(True)
        burning.setChecked(self.burning)
        burning.toggled.connect(self.set_burning)
        menu.addAction('이번 사냥터 메모', self.edit_memo)
        menu.addAction('JEV API 키 설정', self.api_key_settings)
        menu.addAction('배경 투명도', self.opacity_settings)
        menu.addAction('기록 폴더 열기', self.open_records)
        menu.addAction('시작 화면 폴더 열기', self.open_start_captures)
        self.extra_menu_actions(menu)
        menu.addSeparator(); menu.addAction('종료', self.close)
        menu.exec(self.window.mapToGlobal(point))

    def show_history(self):
        from record_history import build_dialog
        self.history_dialog = build_dialog(self)
        self.history_dialog.show(); self.history_dialog.raise_()

    def api_key_settings(self):
        """Enter the LLM Gateway key used by JEV; it is kept only in the OS key store."""
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        import map_name
        has_key = map_name.gateway_key() is not None
        text, ok = QInputDialog.getText(
            self.window, 'JEV API 키',
            ('현재: 저장됨' if has_key else '현재: 없음') + '\n\n'
            'LLM Gateway API 키를 붙여넣어 주세요.\n'
            '이 컴퓨터의 키 저장소(맥 키체인 / 리눅스 키링)에만 저장되고,\n'
            '파일이나 GitHub에는 올라가지 않아요.\n'
            '비워 두고 확인하면 저장된 키를 지워요.',
            QLineEdit.EchoMode.Password)
        if not ok: return
        if not text.strip() and not has_key: return
        try:
            map_name.store_key(text)
        except OSError as exc:
            tracker.log_error('JEV API 키 저장', exc)
            QMessageBox.warning(self.window, 'JEV API 키', f'저장하지 못했어요.\n{exc}')
            return
        saved = map_name.gateway_key() is not None
        self.status.set('JEV API 키 저장됨' if saved else 'JEV API 키 삭제됨')

    def update_jev_button(self):
        self.jev_button.setChecked(self.jev_enabled)
        self.jev_button.setText('JEV 켬' if self.jev_enabled else 'JEV 끔')
        self.jev_button.setToolTip(
            '다음 측정부터 필요할 때 미니맵 이름 부분을 JEV에 보내 확인 · 우클릭 메뉴에서 API 키 설정'
            if self.jev_enabled else 'JEV를 켜면 다음 측정부터 사냥터 이름 확인에 사용')

    def toggle_jev(self, enabled):
        self.jev_enabled = enabled
        self.jev_revision += 1
        self.jev_status_session = None
        self.set_jev_status('')
        self.update_jev_button()
        self.save_config()

    def edit_memo(self):
        from PySide6.QtWidgets import QInputDialog
        from record_history import load_memos, save_memo
        session_id = self.session.session_id
        text, ok = QInputDialog.getText(self.window, '이번 사냥터 메모', '사냥터 (예: 하늘 정원 2층)',
                                        text=load_memos().get(session_id, ''))
        if not ok: return
        try:
            saved = save_memo(session_id, text)
            import map_name
            map_name.learn_correction(session_id, saved)
            self.status.set(f'사냥터 메모 저장 · {saved}' if saved else '사냥터 메모 지움')
        except OSError as exc:
            tracker.log_error('사냥터 메모 저장', exc); self.status.set('사냥터 메모 저장 실패')

    def open_records(self):
        self.open_path(tracker.BASE/'records')

    def select(self):
        if not self.finish_measurement('영역 재지정'): return
        self.cancel_selection()
        if not self.check_permission(): return
        token = self.selection_generation
        self.status.set('게임 창 연결 중 · 영역 선택 이미지 준비')
        def capture():
            try:
                client = self.capture_module().WindowCapture()
                try: image = client.snapshot()
                finally:
                    if hasattr(client, 'close'): client.close()
                self.snapshot_signals.ready.emit(token,image)
            except Exception as exc:
                tracker.log_error('게임 영역 선택',exc)
                self.snapshot_signals.failed.emit(token,str(exc))
        threading.Thread(target=capture,daemon=True).start()

    def present_window_selection(self,token,image):
        if token != self.selection_generation or self.stop.is_set(): return
        dialog = WindowSelection(self,image)
        self.overlays.append(dialog)
        dialog.show(); self.native_overlay(dialog,4); dialog.raise_(); dialog.activateWindow()

    def selection_failed(self,token,error):
        if token == self.selection_generation:
            self.status.set(error)

    def apply_window_region(self,roi,selection_image=None):
        self.window_region = roi
        self.auto_hud = False
        self.level_picker.setEnabled(True)
        previous = self.auto_checkbox.blockSignals(True)
        self.auto_checkbox.setChecked(False)
        self.auto_checkbox.blockSignals(previous)
        self.save_config(); self.reset()
        if selection_image is not None:
            pixel_rect = self.capture_module().pixel_rect
            left,top,width,height=pixel_rect(roi,selection_image.width,selection_image.height)
            self.preview_frame=(selection_image.crop((left,top,left+width,top+height)),time.monotonic())
        self.status.set('게임 영역 저장됨 · 미리보기 확인 후 시작')
        self.cancel_selection()

    def toggle(self):
        if not self.auto_hud and self.window_region is None and self.region is None:
            self.select(); return
        if self.running:
            self.finish_measurement('사용자 정지')
            self.status.set('정지 · 시작 시 새 측정'); return
        if not self.check_permission() or not self.reset(): return
        self.recent_permission = True
        self.running = True
        self.source_label.setText('MapleStory Worlds · 게임 창 캡처')
        self.status.set('레벨·경험치 위치 자동 탐색 중' if self.auto_hud else '게임 창 연결 · 인식기 준비 중')

    def collect_recent(self):
        return self.recent_recording and self.recent_permission and self.auto_hud

    def set_recent_recording(self, enabled):
        self.recent_recording = enabled
        if enabled:
            self.recent_permission = self.has_background_capture_permission()
        self.recent_exp.clear()
        if not self.running:
            self.generation += 1
        self.save_config()

    def finish_measurement(self, reason):
        if not super().finish_measurement(reason):
            return False
        self.recent_start_source = None
        self.recent_exp.clear()
        return True

    def start_with_recent(self):
        now = time.monotonic()
        buffer = self.recent_exp
        if (self.running or not self.collect_recent() or not buffer.available(now)
                or buffer.source[0] != self.generation):
            self.status.set('최근 경험치 증가가 확인되면 포함할 수 있어요')
            return
        source = buffer.source[1:]
        level = source[1]
        session = tracker.Session(level=level)
        for stamp, value in buffer.points:
            session.accept(value, stamp, corroborated=True, baseline_verified=True)
        if session.started is None or session.blocked or session.last != buffer.points[-1][1]:
            self.status.set('최근 경험치 증가분 재확인 중')
            return
        if not self.finish_measurement('최근 5분 포함하여 새 측정'):
            return
        buffer.clear()
        self.session = session
        self.recent_start_source = source
        self.level_var.set(level)
        exp, pct = session.last
        self.value.set(f'{exp:,}  [{pct:.2f}%]')
        gain = exp-session.first[0]
        self.status.set(f'최근 경험치 {gain:,} EXP 포함 · 측정 중')
        try:
            self.save_observation()
        except OSError as exc:
            tracker.log_error('최근 경험치 저장', exc)
            self.save_warning = '자동 저장 실패 · CSV로 별도 보관'
        self.running = True
        self.save_config()

    def worker(self):
        try:
            tracker.MeasurementCore.worker(self)
        finally:
            client = getattr(self.capture_clients, 'client', None)
            if client is not None and hasattr(client, 'close'): client.close()

    def capture_frame(self, legacy_region, *, recent=False):
        if not hasattr(self.capture_clients,'client'):
            self.capture_clients.client = self.capture_module().WindowCapture(**self.CAPTURE_OPTIONS)
        stamp = time.monotonic()
        generation = self.generation
        probe = (not recent and map_name.available() and not self.hunt_activity_lock.locked()
                 and stamp >= getattr(self.capture_clients, 'next_activity_scan', 0))
        starting = not recent and (self.session.started is None or self.recent_start_source is not None)
        include_full = starting or probe
        if self.auto_hud:
            from auto_hud import AutomaticCapture, HudLocator
            if not hasattr(self.capture_clients,'automatic'):
                self.capture_clients.automatic = AutomaticCapture(self.capture_clients.client, HudLocator(self.engine))
            automatic = self.capture_clients.automatic
            if self.auto_rescan.is_set():
                self.auto_rescan.clear()
                automatic.reset()
            image = automatic.read(generation, self.session.level,
                                   not recent and self.session.first is not None, include_full)
        else:
            roi = dict(self.window_region) if self.window_region is not None else None
            image = self.capture_clients.client.snapshot(roi=roi, legacy_region=legacy_region if roi is None else None,
                                                         include_full=include_full)
        if probe:
            self.capture_clients.next_activity_scan = stamp + hunt_activity.SCAN_INTERVAL
            full = image.info.get('full_game_image')
            if full is not None and self.hunt_activity_lock.acquire(blocking=False):
                observed_at = (stamp + time.monotonic()) / 2
                try:
                    level_roi = image.info.get('level_region')
                    style = image.info.get('ui_kind')
                    window = image.info.get('window_id')
                    if not self.character_read_needed(generation, window, stamp):
                        level_roi = None
                    threading.Thread(target=self.read_hunt_activity,
                                     args=(generation, observed_at, full, window, level_roi, style),
                                     name='hunt-activity', daemon=True).start()
                except RuntimeError as exc:
                    self.hunt_activity_lock.release()
                    tracker.log_error('사냥 신호 판독 시작', exc)
        if not starting:
            image.info.pop('full_game_image', None)
        return image

    def read_hunt_activity(self, generation, stamp, frame, window, level_roi, style):
        try:
            observation = hunt_activity.read(frame, self.hunt_activity_cache)
            self.hunt_activity_results.put((generation, stamp, observation))
        except Exception as exc:
            tracker.log_error('사냥 신호 판독', exc)
        try:
            if level_roi is not None:
                identity = character_hud.read(frame, level_roi, style)
                self.character_results.put((generation, window, identity))
        except Exception as exc:
            tracker.log_error('캐릭터 HUD 판독', exc)
        finally:
            self.hunt_activity_lock.release()

    CHARACTER_RECHECK_SECONDS = 300.0

    def character_read_needed(self, generation, window, now):
        """Skip the character OCR once job and name are confirmed for this window."""
        confirmed = ((generation, window) == self.character_source
                     and all(self.character_identity))
        return not confirmed or now >= self.character_recheck_at

    DISCOVERY_PERIOD = 1.0

    def scan_period(self):
        """Look for the HUD every second until the start value is set, then use the normal pace."""
        automatic = getattr(self.capture_clients, 'automatic', None)
        if (self.auto_hud and self.running and self.session.first is None
                and (automatic is None or automatic.hud is None)):
            return min(self.target_seconds, self.DISCOVERY_PERIOD)
        return self.target_seconds

    def open_start_captures(self):
        folder = tracker.BASE/'records'/'start-captures'
        folder.mkdir(parents=True, exist_ok=True)
        self.open_path(folder)

    def save_start_milestones(self, before_first, before_started, observed_at, value, raw, score, details):
        full, frame = details.get('full_game_image'), details.get('image')
        if full is None or frame is None:
            return
        session = self.session
        stages = ['first-frame']
        if before_first is None and session.first is not None:
            stages.append('baseline')
        if before_started is None and session.started is not None:
            stages.append('hunt-start')
        metadata = dict(app_version=tracker.APP_VERSION, level=session.level,
                        measurement_created_wall=session.created_wall,
                        capture_started_wall=details.get('capture_started_wall'),
                        capture_finished_wall=details.get('capture_finished_wall'),
                        observation_monotonic=observed_at,
                        capture_started_monotonic=details.get('capture_started_monotonic'),
                        capture_finished_monotonic=details.get('capture_finished_monotonic'),
                        hunt_started_monotonic=session.started,
                        baseline=session.first, current=session.last, ocr_value=value,
                        gained=session.stats(observed_at)[0], raw=raw, confidence=float(score),
                        variants=details.get('variants', []), status=self.status.get(),
                        backend=details.get('backend'), window_id=details.get('window_id'),
                        window_title=details.get('window_title'), window_bounds=details.get('window_bounds'),
                        window_region=details.get('window_region'), ui_kind=details.get('ui_kind'),
                        game_image_size=details.get('game_image_size'), target_interval=self.target_seconds,
                        capture_interval=self.scan_seconds)
        for stage in stages:
            self.start_captures.submit(session.session_id, stage, full, frame, metadata)
        if session.started is not None:
            self.start_map_reading(session.session_id, full)

    def start_map_reading(self, session_id, full):
        """Read the minimap name once per measurement, off the UI thread."""
        if getattr(self, 'map_read_for', None) == session_id or not map_name.available():
            return
        self.map_read_for = session_id
        revision = self.jev_revision if self.jev_enabled else None

        def can_request():
            return (revision is not None and revision == self.jev_revision
                    and session_id == self.session.session_id and not self.stop.is_set())

        def on_request(has_key):
            if not can_request():
                return False
            status = 'JEV 확인 중…' if has_key else 'JEV API 키 필요 · 우클릭 메뉴에서 설정'
            self.map_results.put((session_id, revision, status))
            return True

        def work():
            raw = picked = None
            try:
                title = map_name.title_area(full)
                raw = map_name.read_title(title)
                if can_request():
                    picked = map_name.choose(raw, title, on_request=on_request)
            except Exception as exc:
                tracker.log_error('사냥터 이름 인식', exc)
            self.map_results.put((session_id, revision, (raw, picked)))
        threading.Thread(target=work, name='map-name', daemon=True).start()

    def set_jev_status(self, text):
        if self.jev_status.text() != text:
            self.jev_status.setText(text)
            self.jev_status.setVisible(bool(text))
            from display_panel import resize
            resize(self)

    def apply_map_results(self):
        from record_history import load_memos, save_memo
        current_session = self.session.session_id
        if self.jev_status_session is not None and self.jev_status_session != current_session:
            self.jev_status_session = None
            self.set_jev_status('')
        while True:
            try: session_id, revision, result = self.map_results.get_nowait()
            except queue.Empty: return
            current_request = self.jev_enabled and revision == self.jev_revision
            if isinstance(result, str):
                if current_request and session_id == current_session:
                    self.jev_status_session = session_id
                    self.set_jev_status(result)
                continue
            raw, picked = result
            if not current_request:
                picked = None
            if (current_request and session_id == self.jev_status_session
                    and self.jev_status.text() == 'JEV 확인 중…'):
                self.set_jev_status(f'JEV로 확인 · {picked}' if picked else 'JEV 결과 없음')
            if not (raw or picked) or load_memos().get(session_id):
                continue  # never overwrite a memo the user already wrote
            try:
                name = save_memo(session_id, picked or map_name.resolve(raw))
                if raw:
                    if picked: map_name.learn_alias(raw, picked)  # same misread next time needs no JEV call
                    map_name.remember_auto(session_id, raw)
                if session_id == current_session:
                    self.status.set(f'사냥터 자동 인식 · {name}')
            except OSError as exc:
                tracker.log_error('사냥터 자동 메모 저장', exc)

    def check_efficiency_change(self):
        """After each new minute, ask once about a confirmed change in efficiency."""
        count = len(self.session.minute_snapshots)
        key = (self.session.session_id, count)
        if key == getattr(self, '_change_checked', None):
            return
        self._change_checked = key
        pending = None
        if self.running and count:
            import segments
            asked = segments.load_tags().get(self.session.session_id, {})
            for change in segments.find_changes(self.session.minute_snapshots):
                if str(change['minute']) not in asked:
                    pending = change
        self.pending_change = pending
        if pending is None:
            self.change_prompt.setVisible(False)
        else:
            direction = '▲' if pending['after'] > pending['before'] else '▼'
            self.change_prompt.setText(
                f"{direction} {pending['minute']}분째부터 분당 {short_exp(pending['before'])} → {short_exp(pending['after'])}"
                ' · 뭐가 달라졌나요? (눌러서 태그)')
            if self.change_prompt.isHidden():
                self.change_prompt.setVisible(True)
                from display_panel import resize
                resize(self)

    def answer_change_prompt(self):
        change = getattr(self, 'pending_change', None)
        if change is None: return
        from PySide6.QtWidgets import QInputDialog
        import segments
        text, ok = QInputDialog.getText(
            self.window, '효율 변화',
            f"{change['minute']}분째부터 분당 {change['before']:,.0f} → {change['after']:,.0f}\n"
            '여기서 뭐가 달라졌나요? (예: 버프 끝, 파티 합류, 맵 이동)\n비워 두고 확인하면 다시 묻지 않아요.')
        if not ok: return
        try:
            segments.save_tag(self.session.session_id, change['minute'], text)
        except OSError as exc:
            tracker.log_error('효율 변화 태그 저장', exc); return
        self._change_checked = None
        self.change_prompt.setVisible(False)
        from display_panel import resize
        resize(self)

    def continue_level_up(self, new_level):
        """Keep the running measurement when the HUD shows a higher level."""
        old = self.session.level
        if not self.session.level_up(new_level, burning=self.burning):
            return False
        self.level_var.set(new_level)
        self.status.set(f'레벨업 Lv.{old} → {new_level} · 측정 계속')
        try: self.save_config()
        except OSError as exc: tracker.log_error('레벨업 설정 저장', exc)
        return True

    def set_burning(self, enabled):
        self.burning = enabled
        self.save_config()

    def cancel_selection(self):
        self.selection_generation += 1
        for overlay in self.overlays: overlay.close()
        self.overlays = []

    def update_capture_label(self):
        if not self.auto_hud:
            self.source_label.setText('MapleStory Worlds · 수동 영역')
            return
        identity = [text for text in reversed(self.character_identity) if text]
        self.source_label.setText(' · '.join([f'Lv. {self.session.level}', *identity]))

    def poll(self):
        if self.worker_ready and self.status.get().startswith('인식기 준비 실패'):
            self.status.set('인식기 준비 완료 · 시작으로 새 측정' if not self.running else '인식기 복구 · 화면 확인 중')
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            generation, value, raw, score, error = event[:5]
            observed_at = event[5] if len(event)>5 else time.monotonic()
            details = event[6] if len(event)>6 else {}
            if generation != self.generation: continue
            if details.get('initialization_error'):
                self.recent_exp.clear()
                self.status.set('인식기 준비 실패 · 2초 후 재시도'); continue
            if details.get('recent'):
                if not self.running and self.collect_recent():
                    if details.get('source_changed'):
                        self.recent_exp.clear()
                    elif value is None:
                        self.recent_exp.readable = False
                    else:
                        self.recent_exp.observe(details['source'], observed_at, value)
                continue
            if not self.running: continue
            if self.recent_start_source is not None and (value is not None or details.get('level_changed')):
                source = (details.get('window_id'), details.get('detected_level'),
                          details.get('content_points'), details.get('ui_kind'))
                if source != self.recent_start_source:
                    reason = '게임 화면 변경 · 포함한 최근 기록 저장'
                    if self.finish_measurement(reason):
                        self.status.set(reason)
                    continue
                self.recent_start_source = None
            self.hud_information.observe(details, self.generation, value, error, time.monotonic())
            if details.get('level_changed'):
                target = details.get('level_to')
                if target is not None and target == self.session.level:
                    continue  # duplicate report from before the level-up was applied
                if self.continue_level_up(target): continue
                self.finish_measurement(str(error))
                self.status.set(str(error))
                continue
            detected_level = details.get('detected_level')
            if self.auto_hud and value and detected_level in tracker.EXP_TABLE:
                if self.session.first is not None and detected_level != self.session.level:
                    if not self.continue_level_up(detected_level):
                        self.finish_measurement('레벨 변경 확인 · 새 측정 필요')
                        self.status.set('레벨 변경 확인 · 기록 보존 · 새 측정 필요')
                        continue
                self.session.level = detected_level
                self.session.total = tracker.EXP_TABLE[detected_level]
                self.level_var.set(detected_level)
            if not details.get('auto_detected') and details.get('window_region') is not None and self.window_region is None:
                self.window_region = details['window_region']
                self.save_config()
            if details.get('backend') in self.CAPTURE_BACKENDS:
                source = (generation, details.get('window_id'))
                if source != self.character_source:
                    self.character_source = source
                    self.character_candidate = (None, None)
                    self.character_identity = (None, None)
                    self.character_recheck_at = 0.0
                self.update_capture_label()
            before = len(self.session.rows)
            before_first, before_started = self.session.first, self.session.started
            if error: self.status.set(str(error)[:80]+' · 자동 재시도 중')
            elif value:
                previous_value = self.session.last
                corroborated = details.get('temporal',{}).get('selected') is not None
                verified_baseline = bool(self.auto_hud and details.get('auto_confirmed') and corroborated)
                state = self.session.accept(value, observed_at, corroborated=corroborated,
                                            baseline_verified=verified_baseline)
                if len(self.session.rows)>before:
                    if previous_value is not None and self.session.last[0] > previous_value[0]:
                        self.hunt_activity.update(generation, observed_at, gained=True)
                    self.remember_accepted_capture(previous_value, details)
                    try: self.save_observation(); self.save_warning = ''
                    except OSError as exc:
                        tracker.log_error('자동 기록 저장', exc)
                        self.save_warning = '자동 저장 실패 · CSV로 별도 보관'; state += ' · 자동 저장 실패'
                self.status.set(f'{state} · 갱신 {self.scan_seconds:.1f}초')
                if self.session.last:
                    exp,pct = self.session.last; self.value.set(f'{exp:,}  [{pct:.2f}%]')
                if self.session.blocked: self.finish_measurement(state)
            else:
                mismatch = None if self.auto_hud else level_mismatch(details, self.session.level, self.session.total)
                self.status.set(mismatch or ('추가 캡처 불일치 · 기존 값 유지' if details.get('temporal',{}).get('checked') else '숫자 판독 보류 · 게임 내부 설명창/영역 확인'))
            if self.auto_hud and not error and value is None:
                self.auto_read_failures += 1
                if self.auto_read_failures >= 3:
                    self.auto_rescan.set()
                    self.auto_read_failures = 0
            elif value:
                self.auto_read_failures = 0
            self.save_start_milestones(before_first,before_started,observed_at,value,raw,score,details)
            try:
                self.save_audit(observed_at,value,raw,score,len(self.session.rows)>before,details)
                self.audit_warning = ''
            except OSError as exc:
                tracker.log_error('진단 기록 저장', exc); self.audit_warning = '진단 저장 실패'
        while True:
            try: key, error = self.start_captures.results.get_nowait()
            except queue.Empty: break
            if error:
                self.capture_failures[key] = error
                tracker.log_error('시작 화면 저장', OSError(error))
            else:
                self.capture_failures.pop(key, None)
        now = time.monotonic()
        gained = self.session.last[0]-self.session.first[0] if self.session.first is not None else 0
        self.total_label.setText(f'{gained:,.0f}')
        self.compact_total.setText(self.total_label.text())
        self.update_rate_display(now)
        if self.session.started is None:
            timer = '기준 확인 중' if self.session.first is None else '첫 증가 대기'
        else:
            active, idle = int(self.session.active_elapsed(now)), int(self.session.idle(now))
            gap = int(self.session.gap_seconds(now))
            timer = f'사냥 {active//3600:02d}:{active%3600//60:02d}:{active%60:02d}'
            if idle:
                timer += f' · {"휴식 중" if self.session.resting(now) else "휴식"} {idle//60}:{idle%60:02d}'
                total = active+idle
                if total >= 60 and not gap: timer += f' · 사냥 {active*100//total}%'
            if gap:
                timer += f' · 공백 {gap//60}:{gap%60:02d}'
        self.timer_label.setText(timer)
        while not self.hunt_activity_results.empty():
            generation, stamp, observation = self.hunt_activity_results.get_nowait()
            if self.running and generation == self.generation:
                self.hunt_activity.update(generation, stamp, observation)
        self.activity_label.setText(self.hunt_activity.text(self.generation, now) if self.running
                                    else '사냥 신호 · 측정 대기')
        while not self.character_results.empty():
            generation, window, current = self.character_results.get_nowait()
            if not self.running or (generation, window) != self.character_source:
                continue
            self.character_identity = tuple(
                reading if reading and reading == candidate else None
                for reading, candidate in zip(current, self.character_candidate))
            self.character_candidate = current
            if all(self.character_identity):
                self.character_recheck_at = time.monotonic() + self.CHARACTER_RECHECK_SECONDS
            self.update_capture_label()
            nickname = self.character_identity[1]
            session = self.session
            if nickname and not session.character_conflict and nickname != session.character_name:
                session.character_conflict = session.character_name is not None
                session.character_name = None if session.character_conflict else nickname
                if session.rows:
                    try:
                        self.save_summary()
                    except OSError as exc:
                        tracker.log_error('사냥 기록 캐릭터 저장', exc)
        self.check_efficiency_change()
        self.apply_map_results()
        snapshot = self.session.minute_result()
        if snapshot:
            one, six = snapshot['one'], snapshot['six']
            index = snapshot['index']
            self.recent_minute.setText(f'{one:,.0f}' if one is not None else '미확정')
            # Before 6 full minutes exist, project the partial total to 6 minutes.
            projected = six is not None and 0 < index < 6
            if six is None: six_text = '미확정'
            elif projected:
                # The timer starts on the first gain, so that packet has no hunting time
                # of its own; scale only the timed gains (a real 6-minute total excludes it).
                timed = max(0.0, six-self.session.first_gain)
                six_text = f'{timed*6/index:,.0f} 예상'
            else: six_text = f'{six:,.0f}'
            self.recent_six.setText(six_text)
            self.recent_six.setToolTip(f'{index}분 실제 {six:,.0f} − 첫 증가 {self.session.first_gain:,.0f}, × 6/{index} · 6분 예상치' if projected else '')
            remaining = max(0, math.ceil((snapshot['index']+1)*60-self.session.elapsed(now)))
            quality = '관측 공백 추정' if snapshot['estimated'] else '분별 집계'
            if snapshot['one'] is None or snapshot['six'] is None:
                info = f'{index}:00 집계 · 공백 포함 구간 미확정'
            else:
                info = f'{quality} {snapshot["index"]}:00 · 다음 {remaining}초' if self.running else f'{snapshot["index"]}:00 집계 · 측정 정지'
            if (self.running and snapshot['one'] is not None and snapshot['six'] is not None
                    and self.session.elapsed(now) >= (snapshot['index']+1)*60):
                info = f'집계 {snapshot["index"]}:00 유지 · 새 관측 대기'
            if projected:
                info += f' · 6분은 {index}분 기준 예상'
            self.minute_info.setText(info)
        else:
            text = '첫 1분 대기' if self.session.started is not None else '집계 대기'
            self.recent_minute.setText(text); self.recent_six.setText(text)
            remaining = max(0,60-int(self.session.elapsed(now)))
            self.minute_info.setText(f'첫 집계까지 {remaining}초 · 1분마다 갱신' if self.session.started is not None else '첫 증가 후 1분마다 집계')
        warning = self.status.get()
        if self.running and warning.startswith('레벨 설정 확인'):
            footer = f'레벨 설정 확인 · Lv.{self.session.level}'
        elif self.session.blocked:
            footer = '감소 충돌 · 새 측정 필요'
        elif not self.running:
            footer = '측정 대기' if self.session.first is None else '이전 기록 · 측정 정지' if self.session.restored else '측정 정지'
        elif self.session.started is None and self.session.first:
            footer = '준비 완료 · 첫 경험치 증가 대기'
        elif '재시도' in warning or '오류' in warning:
            footer = '캡처 재시도 중'
        else:
            footer = f'갱신 {self.scan_seconds:.1f}초 · 목표 {self.target_seconds:.1f}초' if self.session.first else '숫자 확인 중'
        delayed = bool(self.session.samples and now-self.session.samples[-1][0] > 5)
        if self.running and delayed:
            footer = f'인식 지연 · {int(now-self.session.samples[-1][0])}초 전 값'
        if not self.session.blocked and (self.save_warning or self.audit_warning):
            footer = self.save_warning or self.audit_warning
        if not self.session.blocked and any(key[0] == self.session.session_id for key in self.capture_failures):
            footer = '시작 화면 저장 실패 · 기록 폴더/용량 확인'
        self.footer.setText('● '+footer)
        self.footer.setToolTip(' · '.join(filter(None,[self.session.stop_reason,self.save_warning,self.audit_warning]))
                               if self.session.blocked else warning)
        self.compact_total.setToolTip('확인 필요 · '+self.session.stop_reason if self.session.blocked else '현재 확정 경험치 − 시작 기준 경험치')
        footer_style = 'color: '+('#e7b86e' if self.session.blocked or (self.running and delayed) else '#a8edbb' if self.running else '#7c8c9d')+';'
        if footer_style != self.footer.styleSheet():
            self.footer.setStyleSheet(footer_style)
        self.current_label.setText(self.value.get())
        self.baseline_label.setText(f'시작 기준 {self.session.first[0]:,}' if self.session.first else '시작 기준 —')
        checked = str(self.session.level) in tracker.EXP_METADATA.get('screenshot_verified',{})
        if self.session.level is not None and self.session.level in tracker.EXP_TABLE:
            table_text = (f'남음 {self.session.total-self.session.last[0]:,} · 통 {self.session.total:,}'
                          if self.session.last else f'{self.session.level}레벨 통 {self.session.total:,} · '+('사진 대조' if checked else '참고표'))
        else: table_text = '경험치통 추정치 사용'
        self.table_label.setText(table_text)
        self.detail_label.setText(self.status.get())
        ui_text = self.hud_information.text(running=self.running, automatic=self.auto_hud,
                                            generation=self.generation, now=now)
        self.ui_label.setText(ui_text)
        self.title_label.setToolTip(ui_text)
        self.toggle_button.setText('측정 정지' if self.running else '측정 시작')
        self.update_recent_button(now)
        if self.preview_frame is not None and self.preview_frame[1] != self.preview_stamp:
            frame, self.preview_stamp = self.preview_frame
            frame = frame.convert('RGB')
            qimage = QImage(frame.tobytes(), frame.width, frame.height, frame.width*3, QImage.Format.Format_RGB888).copy()
            self.preview.setPixmap(QPixmap.fromImage(qimage).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

    def update_recent_button(self, now):
        available = (not self.running and self.collect_recent() and self.recent_exp.available(now)
                     and self.recent_exp.source[0] == self.generation)
        self.recent_button.setEnabled(available)
        visible = not self.running and self.auto_hud and self.recent_recording
        if self.recent_button.isHidden() == visible:
            self.recent_button.setVisible(visible)
            from display_panel import resize
            resize(self)
        if available:
            first, last = self.recent_exp.points[0], self.recent_exp.points[-1]
            self.recent_button.setToolTip(f'최근 {int(last[0]-first[0])}초 · {last[1][0]-first[1][0]:,} EXP로 측정을 시작해요')
        elif not self.recent_permission:
            self.recent_button.setToolTip('측정 시작으로 화면 기록 권한을 확인해 주세요')
        else:
            self.recent_button.setToolTip('같은 창과 레벨에서 경험치 증가를 확인하면 포함할 수 있어요')

    def save_config(self):
        try:
            temporary = tracker.BASE/'settings.json.tmp'
            temporary.write_text(json.dumps({
                'region': self.region, 'window_region': self.window_region,
                'position': [self.window.x(), self.window.y()], 'level': self.session.level,
                'compact': self.compact, 'always_on_top': self.always_on_top,
                'window_opacity': self.window_opacity, 'auto_hud': self.auto_hud,
                'recent_recording': self.recent_recording, 'burning': self.burning,
                'jev_enabled': self.jev_enabled,
            }), encoding='utf-8')
            temporary.replace(tracker.BASE/'settings.json')
        except OSError as exc:
            tracker.log_error('설정 저장', exc); self.status.set('창 위치/영역 저장 실패')

    def export(self):
        target, _ = QFileDialog.getSaveFileName(self.window, 'CSV 저장', str(Path.home()/'Documents'/'planet-exp.csv'), 'CSV (*.csv)')
        if not target: return
        temporary = None
        try:
            destination = Path(target)
            with tempfile.NamedTemporaryFile('w',newline='',encoding='utf-8-sig',dir=destination.parent,prefix='.planet-exp-',suffix='.tmp',delete=False) as stream:
                temporary = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(['관측 시각','현재 경험치','퍼센트','사냥 초','총 획득 경험치','상태'])
                writer.writerows(self.session.rows)
            temporary.replace(destination); self.status.set('CSV 저장 완료')
        except OSError as exc:
            tracker.log_error('CSV 저장',exc); self.status.set('CSV 저장 실패 · 저장 위치/권한 확인')
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)

    def close(self): self.window.close()

    def shutdown(self):
        self.key_repeat.shutdown()
        if not self.finish_measurement('앱 종료'): return False
        self.save_config(); self.cancel_selection(); self.stop.set(); self.timer.stop()
        self.start_captures.close()
        self.application.quit(); return True


def run_app(controller_class):
    """Command-line entry shared by both platforms."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test-report', type=Path)
    parser.add_argument('--offline-test-report', type=Path)
    parser.add_argument('--auto-test-report', type=Path)
    parser.add_argument('--start-measurement', action='store_true')
    args = parser.parse_args()
    application = QApplication([sys.argv[0]])
    application.setApplicationName(controller_class.APP_TITLE)
    application.setDesktopFileName('planet-helper')
    application.setQuitOnLastWindowClosed(False)
    from unified_data import DataLock
    diagnostic_data = None
    if args.self_test_report or args.offline_test_report or args.auto_test_report:
        # Diagnostics must never write real user records, including when launched
        # from Finder without a separately supplied environment variable.
        diagnostic_data = tempfile.TemporaryDirectory(prefix='planet-helper-test-')
        tracker.BASE = Path(diagnostic_data.name)
    lock = DataLock(tracker.BASE)
    if not lock.acquire():
        QMessageBox.information(None, controller_class.APP_TITLE, '이미 실행 중이에요. 열려 있는 플래닛 도우미 창을 사용해 주세요.')
        return
    controller = None
    try:
        if args.auto_test_report:
            from auto_checks import run
            run(args.auto_test_report)
            return
        if args.offline_test_report:
            from unified_diagnostics import run_offline_diagnostics
            run_offline_diagnostics(application, args.offline_test_report)
            return
        if args.self_test_report:
            from qt_diagnostics import run_diagnostics
            run_diagnostics(application,args.self_test_report)
            return
        controller = controller_class(application)
        def exception_handler(kind, value, tb):
            import traceback
            (tracker.BASE/'callback-error.log').write_text(''.join(traceback.format_exception(kind,value,tb)),encoding='utf-8')
            tracker.log_error('화면 동작',value)
            controller.status.set('화면 동작 오류 · callback-error.log 확인')
        sys.excepthook = exception_handler
        import signal
        signal.signal(signal.SIGTERM, lambda *_: controller.close())
        signal.signal(signal.SIGINT, lambda *_: controller.close())
        controller.show()
        if args.start_measurement and not controller.running:
            controller.toggle()
        application.exec()
    finally:
        if controller is not None:
            controller.key_repeat.shutdown()
            if not controller.stop.is_set():
                controller.stop.set()
                controller.timer.stop()
                controller.start_captures.close()
        lock.close()
        if diagnostic_data is not None:
            diagnostic_data.cleanup()
