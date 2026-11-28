"""Qt controller shared by the Linux and macOS apps.

Platform modules (linux_app.py, mac_app.py) subclass SharedController and only
override the hooks marked "Platform hook" below; every other change made here
applies to both apps.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QTimer, QObject, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, QShortcut, QKeySequence, QIcon
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QFileDialog, QMessageBox, QMenu, QMenuBar, QDialog, QComboBox)
from controller_jev import JevMixin
from controller_menus import MenuAndSettingsMixin
from controller_probes import MesoAndProbesMixin
import character_hud
import exp_buffs
import hunt_activity
import jev
import jev_assist
import map_name
import meso
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


def set_exp_stat(label, text, explanation):
    if len(text) > len('999,999,999') and text.replace(',', '').isdigit():
        label.setText(short_exp(int(text.replace(',', ''))))
        label.setToolTip(f'{text} EXP · {explanation}')
    else:
        label.setText(text)
        label.setToolTip(explanation)


def arrival_clock(minutes, now):
    """Wall-clock time of the predicted level-up: 15:42, 내일 01:20 or 11/22 03:00."""
    arrival = now + timedelta(minutes=minutes)
    days = (arrival.date()-now.date()).days
    clock = arrival.strftime('%H:%M')
    if days == 0: return clock
    if days == 1: return f'내일 {clock}'
    return f'{arrival.month}/{arrival.day} {clock}'


def level_eta_text(rate, session, *, compact=False, now=None):
    """Hourly EXP and time to the next level from the cumulative per-minute average."""
    level = f'Lv.{session.level} ' if session.level else ''
    if rate['status'] != 'ready' or not rate['cumulative'] or not session.total or not session.last:
        return f'{level}레벨업 예상 —' if compact else f'{level}레벨업 예상 · 평균 집계 후 표시'
    remaining = max(0, session.total-session.last[0])
    minutes = math.ceil(remaining/rate['cumulative'])
    eta = f'{minutes//60}시간 {minutes%60}분' if minutes >= 60 else f'{minutes}분'
    clock = arrival_clock(minutes, now or datetime.now())
    if compact:
        return f'{level}시간당 {short_exp(rate["cumulative"]*60)} · 레벨업 {eta} ({clock})'
    ups = f' · 레벨업 {len(session.level_ups)}회' if session.level_ups else ''
    return (f'{level}시간당 {short_exp(rate["cumulative"]*60)} · 레벨업까지 {eta} · {clock}쯤 '
            f'({remaining/session.total*100:.1f}% 남음){ups}')


class TrackerWindow(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.controller = controller
        self.drag_origin = None

    RADIUS = 16

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Over a blurred background a lighter tint keeps the glass look; without blur
        # the tint alone carries the chosen opacity.
        strength = .62 if getattr(self.controller, 'glass', False) else 1.0
        painter.setBrush(QColor(16, 22, 32, round(self.controller.window_opacity*255*strength)))
        painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), self.RADIUS, self.RADIUS)


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


class CloudSignals(QObject):
    done = Signal(str)  # status text, delivered on the UI thread


class SnapshotSignals(QObject):
    ready = Signal(int,object)
    failed = Signal(int,str)


class WindowImage(QWidget):
    def __init__(self, dialog, image, mode='exp'):
        super().__init__(dialog)
        self.dialog = dialog
        self.mode = mode
        self.start = self.end = None
        rgb = image.convert('RGB')
        self.pixmap = QPixmap.fromImage(QImage(rgb.tobytes(),rgb.width,rgb.height,rgb.width*3,QImage.Format.Format_RGB888).copy())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName('인벤토리 메소 영역 선택 이미지' if mode == 'meso' else '게임 경험치 영역 선택 이미지')

    def paintEvent(self,event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(),self.pixmap,self.pixmap.rect())
        if self.start is not None:
            painter.setPen(QPen(QColor('#fbbf77' if self.mode == 'meso' else '#ff5555'),2))
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
        min_width = 24 if self.mode == 'meso' else 40
        if rect.width()/self.width()*points[0] < min_width or rect.height()/self.height()*points[1] < 8:
            self.dialog.instruction.setText(
                '선택이 너무 작아요 · 인벤토리 메소 숫자 칸 전체를 드래그해 주세요'
                if self.mode == 'meso' else '선택이 너무 작아요 · 경험치 숫자와 [퍼센트]를 함께 선택해 주세요')
            return
        roi = dict(left=rect.x()/self.width(),top=rect.y()/self.height(),
                   width=rect.width()/self.width(),height=rect.height()/self.height(),
                   reference_aspect=points[0]/points[1])
        if self.mode == 'meso':
            self.dialog.controller.apply_meso_region(roi,self.dialog.image)
        else:
            self.dialog.controller.apply_window_region(roi,self.dialog.image)


class WindowSelection(QDialog):
    def __init__(self,controller,image,mode='exp'):
        super().__init__(controller.window,Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.controller,self.image,self.mode = controller,image,mode
        self.setWindowTitle('인벤토리 메소 영역 지정' if mode == 'meso' else '게임 경험치 영역 지정')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        self.instruction = QLabel(
            '게임 창 이미지에서 인벤토리 메소 숫자 칸을 드래그 · ESC 취소'
            if mode == 'meso' else '게임 창 이미지에서 경험치 숫자와 [퍼센트]만 드래그 · ESC 취소')
        layout.addWidget(self.instruction)
        self.image_view = WindowImage(self,image,mode=mode)
        available = controller.application.primaryScreen().availableGeometry()
        factor = min((available.width()-80)/image.width,(available.height()-140)/image.height,1)
        self.image_view.setFixedSize(round(image.width*factor),round(image.height*factor))
        layout.addWidget(self.image_view)
        self.setFixedSize(self.sizeHint())
        self.move(available.center()-self.rect().center())


class SharedController(MenuAndSettingsMixin, MesoAndProbesMixin, JevMixin, tracker.MeasurementCore):
    # ---- Platform hooks (override in linux_app.py / mac_app.py) ----
    APP_TITLE = f'플래닛 도우미 {tracker.APP_VERSION}'
    NATIVE_MENU_BAR = False
    CAPTURE_MODULE = 'linux_capture'
    CAPTURE_OPTIONS = {}
    CAPTURE_BACKENDS = ()
    REPEAT_NEEDS_PERMISSION = False

    def native_overlay(self, widget, level=3):
        """Keep a window above others / on all Spaces where the platform needs it."""

    def apply_glass(self, widget):
        """Blur what is behind the window; return True when the platform did it."""
        return False

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
            self.glass = self.apply_glass(self.window)
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
        self.meso_preview_frame = self.meso_preview_stamp = None
        self.meso_diag = None
        self.save_warning = self.audit_warning = ''
        self.worker_ready = False
        self.stop = threading.Event()
        self.overlays = []
        self.selection_generation = 0
        self.selection_mode = 'exp'
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
        self.buff_results = queue.SimpleQueue()
        self.buff_confirmation = exp_buffs.Confirmation()
        self.buff_window = None
        self.buff_detector = exp_buffs.Detector()
        self.buff_has_templates = exp_buffs.available(tracker.BASE)
        self.buff_revision = 0
        self.buff_last_flush = float('-inf')
        self.hunt_activity_lock = threading.Lock()
        self.hunt_activity_cache = {}
        self.character_results = queue.SimpleQueue()
        # One JEV question at a time for every feature; nothing is sent while JEV is off.
        self.jev_arbiter = jev.Arbiter(
            lambda: self.jev_enabled,
            full_screen_enabled=lambda: self.jev_full_screen,
        )
        self.jev_frames = jev_assist.FrameRing()
        self.jev_hud_missing = None
        self.jev_stall_saved = None
        self.meso_reader = meso.Reader(self.jev_arbiter)
        self.meso_capture_lock = threading.Lock()
        self.meso_results = queue.SimpleQueue()
        self.meso_confirmation = meso.Confirmation()
        self.idle_meso_confirmation = meso.Confirmation()
        self.idle_meso_source = None
        self.idle_meso = None
        self.meso_rechecking = False
        self.meso_log = meso.DailyLog()
        self.meso_source = None
        self.meso_opening = 0
        self.meso_visible = False
        self.meso_reader_source = None
        self.meso_last_flush = float('-inf')
        self._probe_executor = None
        self._probe_executor_lock = threading.Lock()
        self.character_candidate = (None, None)
        self.character_identity = (None, None)
        self.character_source = None
        self.character_recheck_at = 0.0
        self.nickname_sample = None
        config, warning = tracker.load_settings(tracker.BASE/'settings.json') if restore else ({}, None)
        self.auto_hud = config.get('auto_hud', True)
        self.recent_recording = config.get('recent_recording', True)
        self.burning = config.get('burning', False)
        self.auto_measure = config.get('auto_measure', False)
        self.auto_start_hold = None
        self.jev_enabled = config.get('jev_enabled', False)
        self.jev_full_screen = config.get('jev_full_screen', True)
        self.current_character = config.get('current_character')
        self.nickname_patterns = config.get('nickname_patterns', {})
        self.meso_trade_threshold = config.get('meso_trade_threshold', meso.TRADE_THRESHOLD)
        self.meso_region = config.get('meso_region')
        if self.meso_region is not None:
            self.meso_reader.set_manual_roi(self.meso_region)
        self.cloud_remote = config.get('cloud_remote')
        self.cloud_lock = threading.Lock()
        self.cloud_signals = CloudSignals()
        self.cloud_signals.done.connect(self.cloud_finished)
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
        self.update_capture_label()
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

    def _monotonic(self):
        return time.monotonic()

    def _start_thread(self, *, target, name):
        threading.Thread(target=target, name=name, daemon=True).start()

    def submit_probe(self, fn, *args):
        with self._probe_executor_lock:
            if self.stop.is_set():
                raise RuntimeError('Controller is stopping')
            if self._probe_executor is None:
                self._probe_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='probe')
            self._probe_executor.submit(fn, *args)

    def _shutdown_probes(self):
        with self._probe_executor_lock:
            executor, self._probe_executor = self._probe_executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

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
        from display_panel import resize
        resize(self)

    def toggle_display(self):
        self.compact = not self.compact
        from display_panel import resize
        resize(self)
        self.save_config()

    def toggle_pin(self, checked):
        enabled = self.apply_pin(bool(checked))
        self.always_on_top = enabled
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
        set_exp_stat(self.minute_label, average, info)
        set_exp_stat(self.compact_average, average, info)
        set_exp_stat(self.six_label, recent, '최근 3분 관측 기준의 분당 평균 경험치')
        self.rate_info.setText(info)
        self.level_eta.setText(level_eta_text(rate, self.session))
        self.compact_level_eta.setText(level_eta_text(rate, self.session, compact=True))
        self.compact_level_eta.setToolTip(self.level_eta.text())

    def update_window_stats(self, now, hunted):
        """Last 1 and 6 minutes of hunting time, refreshed every 10 hunting seconds."""
        session = self.session
        six_heading = '최근 6분 획득'
        six_tip = '휴식(3분 이상 경험치 정지)을 뺀 사냥 시간 기준 최근 6분 획득량'
        projected = False
        one = session.hunt_gain(60, now) if hunted >= 60 else None
        six = session.hunt_gain(360, now) if hunted >= 60 else None
        if one is None or six is None:
            one_text = six_text = '첫 1분 대기' if session.started is not None else '—'
            self.minute_info.setText(f'첫 집계까지 {max(0, math.ceil(60-hunted))}초 · 사냥 시간 기준'
                                     if session.started is not None else '첫 증가 후 사냥 시간 기준으로 집계')
        else:
            one_text = f'{one[0]:,.0f}'
            six_value = six[0]
            # Before 6 hunting minutes exist, project the partial total to 6 minutes.
            if six[1] < 360:
                projected = True
                six_value = six[0]*360/six[1]
                six_heading = f'6분 예상 · {int(six[1]//60)}/6분'
                six_tip = f'사냥 시간 {int(six[1]//60)}분 동안 얻은 경험치를 6분으로 환산한 예상치'
            six_text = f'{six_value:,.0f}'
            rate = session.rate_estimate(now)
            if not projected and rate['status'] == 'ready' and rate['cumulative']:
                change = six_value/(rate['cumulative']*6)-1
                if abs(change) >= .05:
                    six_heading += f" {'▲' if change > 0 else '▼'}{abs(change)*100:.0f}%"
                    six_tip += f' · 누적 평균보다 {abs(change)*100:.0f}% {"높음" if change > 0 else "낮음"}'
            info = '사냥 시간 기준 · 3분 이상 경험치 정지는 휴식으로 제외 · 10초마다 갱신'
            if projected:
                info += f' · 6분은 {int(six[1]//60)}분 기준 예상'
            self.minute_info.setText(info)
        for widget in (self.recent_minute, self.compact_recent_minute):
            set_exp_stat(widget, one_text, '휴식을 뺀 사냥 시간 기준 최근 1분 획득량')
        for heading, widget in ((self.six_heading, self.recent_six),
                                (self.compact_six_heading, self.compact_recent_six)):
            heading.setText(six_heading)
            widget.setAccessibleName(six_heading)
            set_exp_stat(widget, six_text, six_tip)
            style = 'color:#9fb5c8;' if projected else ''
            if widget.styleSheet() != style:
                widget.setStyleSheet(style)

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

    @staticmethod
    def label(text, name='muted'):
        label = QLabel(text); label.setObjectName(name)
        return label

    @staticmethod
    def line():
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet('color: #2b3542;'); return line

    def show(self):
        if not getattr(self, 'auto_backup_started', False):
            self.auto_backup_started = True
            QTimer.singleShot(5000, self.run_auto_backup)
        self.window.show()
        self.native_overlay(self.window, 3 if self.always_on_top else 0)
        self.glass = self.apply_glass(self.window)
        self.window.raise_()
        self.window.activateWindow()

    def select(self):
        if not self.finish_measurement('영역 재지정'): return
        self._start_window_selection('exp', '게임 창 연결 중 · 영역 선택 이미지 준비')

    def select_meso_region(self):
        self._start_window_selection('meso', '게임 창 연결 중 · 메소 영역 선택 이미지 준비')

    def _start_window_selection(self, mode, status_text):
        self.cancel_selection()
        if not self.check_permission(): return
        self.selection_mode = mode
        token = self.selection_generation
        self.status.set(status_text)
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
        dialog = WindowSelection(self,image,mode=getattr(self,'selection_mode','exp'))
        self.overlays.append(dialog)
        dialog.show(); self.native_overlay(dialog,4); dialog.raise_(); dialog.activateWindow()

    def selection_failed(self,token,error):
        if token == self.selection_generation:
            self.status.set(error)

    def apply_meso_region(self, roi, selection_image=None):
        self.meso_region = roi
        self.meso_reader_source = None
        self.meso_reader.set_manual_roi(roi)
        self.save_config()
        if selection_image is not None:
            pixel_rect = self.capture_module().pixel_rect
            left, top, width, height = pixel_rect(roi, selection_image.width, selection_image.height)
            self.meso_preview_frame = (selection_image.crop((left, top, left + width, top + height)), time.monotonic())
        self.status.set('메소 수동 영역 저장됨 · 인벤토리가 열리면 지정 영역에서 읽어요')
        self.cancel_selection()

    def clear_meso_region(self):
        self.meso_region = None
        self.meso_reader_source = None
        self.meso_reader.set_manual_roi(None)
        self.save_config()
        self.status.set('메소 영역 자동 탐색으로 되돌림')

    def apply_window_region(self,roi,selection_image=None):
        self.window_region = roi
        self.auto_hud = False
        self.level_picker.setEnabled(True)
        previous = self.auto_checkbox.blockSignals(True)
        self.auto_checkbox.setChecked(False)
        self.auto_checkbox.blockSignals(previous)
        self.save_config(); self.reset()
        from display_panel import resize
        resize(self)
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
            if self.auto_measure:
                # Do not restart the measurement the user just stopped.
                self.auto_start_hold = time.monotonic()
                self.status.set('정지 · 경험치가 1분 멈춘 뒤부터 자동 시작')
            else:
                self.status.set('정지 · 시작 시 새 측정')
            return
        self.auto_start_hold = None
        if not self.check_permission() or not self.reset(): return
        self.recent_permission = True
        self.running = True
        self.update_capture_label()
        self.status.set('레벨·경험치 위치 자동 탐색 중' if self.auto_hud else '게임 창 연결 · 인식기 준비 중')

    def collect_recent(self):
        return self.recent_recording and self.recent_permission and self.auto_hud

    AUTO_START_GAINS = 2
    AUTO_START_WINDOW = 60.0
    AUTO_END_SECONDS = 300.0
    AUTO_RESTART_QUIET = 60.0  # EXP quiet this long after an auto end re-arms auto start
    auto_measure = False
    auto_start_hold = None

    def set_auto_measure(self, enabled):
        self.auto_measure = enabled
        self.auto_start_hold = None
        if enabled and not self.recent_recording:
            self.set_recent_recording(True)  # automatic start reads the idle EXP buffer
        else:
            self.save_config()

    def auto_measure_tick(self, now):
        """Start on repeated EXP gains and end after a long observed pause."""
        if not self.auto_measure:
            return
        if self.running:
            session = self.session
            if (session.started is not None and session.last_gain_at is not None and session.samples
                    and session.samples[-1][0]-session.last_gain_at >= self.AUTO_END_SECONDS):
                reason = f'경험치 {int(self.AUTO_END_SECONDS//60)}분 정지 · 자동 종료'
                if self.finish_measurement(reason):
                    self.status.set(reason+' · 다시 오르면 새 측정')
            return
        buffer = self.recent_exp
        usable = self.collect_recent() and buffer.source is not None and buffer.source[0] == self.generation
        gains = buffer.increase_stamps() if usable else []
        if self.auto_start_hold is not None:
            if gains and gains[-1] > self.auto_start_hold:
                self.auto_start_hold = gains[-1]
            if now-self.auto_start_hold < self.AUTO_RESTART_QUIET:
                return
            self.auto_start_hold = None
            buffer.clear()  # gains from before the pause belong to the stopped hunt
            return
        recent_gains = [stamp for stamp in gains if now-stamp <= self.AUTO_START_WINDOW]
        if len(recent_gains) >= self.AUTO_START_GAINS and buffer.available(now):
            self.start_with_recent()

    def set_recent_recording(self, enabled):
        self.recent_recording = enabled
        if not enabled:
            self.auto_measure = False
        if enabled:
            self.recent_permission = self.has_background_capture_permission()
        self.recent_exp.clear()
        if not self.running:
            self.generation += 1
        self.save_config()

    def finish_measurement(self, reason):
        if self.running and not self.session.restored and hasattr(self, 'meso_results'):
            now = time.monotonic()
            self.apply_meso_results(now, finishing=True)
            self.session.meso.finish(self.meso_source, now)
            self.update_meso_display(now)
        had_record = bool(self.session.rows) and not self.session.restored
        if not super().finish_measurement(reason):
            return False
        self.recent_start_source = None
        self.recent_exp.clear()
        # Share a finished hunt right away; at app exit a daemon upload could be cut off.
        if had_record and reason != '앱 종료' and getattr(self, 'cloud_remote', None):
            self.start_cloud_sync(push_only=True)
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
        session.meso_start_at = now
        self.seed_meso_from_idle(self.generation, source[0], now)
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
        session = self.session
        models_available = map_name.available()
        probe = ((models_available or (not recent and self.buff_has_templates))
                 and not self.hunt_activity_lock.locked()
                 and stamp >= getattr(self.capture_clients, 'next_activity_scan', 0)
                 and (not recent or stamp >= self.character_recheck_at))
        meso_probe = (models_available and not self.meso_capture_lock.locked()
                      and ((not recent and session.meso_start_at is not None and session.meso_start_frame is None)
                           or (recent and self.collect_recent()))
                      and (generation != getattr(self.capture_clients, 'meso_generation', None)
                           or stamp >= getattr(self.capture_clients, 'next_meso_scan', 0)))
        starting = not recent and (session.started is None or self.recent_start_source is not None)
        include_full = starting or probe or meso_probe
        if self.auto_hud:
            from auto_hud import AutomaticCapture, HudLocator, HudNotFound
            if not hasattr(self.capture_clients,'automatic'):
                self.capture_clients.automatic = AutomaticCapture(self.capture_clients.client, HudLocator(self.engine))
            automatic = self.capture_clients.automatic
            if self.auto_rescan.is_set():
                self.auto_rescan.clear()
                automatic.reset()
            try:
                image = automatic.read(generation, self.session.level,
                                       not recent and self.session.first is not None, include_full)
            except HudNotFound:
                if not recent:
                    self.note_missing_hud(generation, getattr(automatic, 'last_full', None))
                raise
            self.jev_hud_missing = None
        else:
            roi = dict(self.window_region) if self.window_region is not None else None
            image = self.capture_clients.client.snapshot(roi=roi, legacy_region=legacy_region if roi is None else None,
                                                         include_full=include_full)
        full = image.info.get('full_game_image')
        observed_at = (stamp + time.monotonic()) / 2
        window = image.info.get('window_id')
        if meso_probe and full is not None and window is not None:
            if self.queue_meso_frame(generation, observed_at, full, window):
                self.capture_clients.meso_generation = generation
                waiting_for_start = (not recent and session.meso.start is None
                                     and session.meso_start_at is not None
                                     and stamp - session.meso_start_at <= meso.START_WINDOW)
                self.capture_clients.next_meso_scan = stamp + self.meso_scan_interval(waiting_for_start)
        if probe:
            self.capture_clients.next_activity_scan = stamp + hunt_activity.SCAN_INTERVAL
            if full is not None and self.hunt_activity_lock.acquire(blocking=False):
                try:
                    level_roi = image.info.get('level_region')
                    style = image.info.get('ui_kind')
                    if not self.character_read_needed(generation, window, stamp):
                        level_roi = None
                    self.submit_probe(self.read_hunt_activity,
                                      generation, observed_at, full, window, level_roi, style, recent)
                except RuntimeError as exc:
                    self.hunt_activity_lock.release()
                    tracker.log_error('사냥 신호 판독 시작', exc)
        if not starting:
            image.info.pop('full_game_image', None)
        return image

    DISCOVERY_PERIOD = 1.0

    def scan_period(self):
        """Look for the HUD every second until the start value is set, then use the normal pace."""
        automatic = getattr(self.capture_clients, 'automatic', None)
        if (self.auto_hud and self.running and self.session.first is None
                and (automatic is None or automatic.hud is None)):
            return min(self.target_seconds, self.DISCOVERY_PERIOD)
        return self.target_seconds

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
                        self.set_character_source(None)
                    elif value is None:
                        self.recent_exp.readable = False
                    else:
                        self.recent_exp.observe(details['source'], observed_at, value)
                        self.set_character_source(details['source'][:2])
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
            if not error and details.get('backend') in self.CAPTURE_BACKENDS:
                self.buff_window = (generation, details.get('window_id'))
            if details.get('backend') in self.CAPTURE_BACKENDS:
                source = (generation, details.get('window_id'))
                self.set_character_source(source)
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
                if before_first is None and self.session.first is not None:
                    self.start_meso_capture(generation, observed_at, details)
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
        total_tip = ('확인 필요 · '+self.session.stop_reason if self.session.blocked
                     else '현재 확정 경험치 − 시작 기준 경험치')
        set_exp_stat(self.total_label, f'{gained:,.0f}', total_tip)
        self.update_rate_display(now)
        hunt_time = '00:00:00'
        if self.session.started is None:
            timer = '기준 확인 중' if self.session.first is None else '첫 증가 대기'
        else:
            active, idle = int(self.session.active_elapsed(now)), int(self.session.idle(now))
            gap = int(self.session.gap_seconds(now))
            hunt_time = f'{active//3600:02d}:{active%3600//60:02d}:{active%60:02d}'
            timer = ''
            if idle:
                timer = f'{"휴식 중" if self.session.resting(now) else "휴식"} {idle//60}:{idle%60:02d}'
                total = active+idle
                if total >= 60 and not gap: timer += f' · 사냥 {active*100//total}%'
            if gap:
                timer = ' · '.join(filter(None,(timer,f'공백 {gap//60}:{gap%60:02d}')))
        for widget in (self.timer_label, self.compact_time):
            widget.setText(hunt_time)
            widget.setToolTip('순수 사냥 ' + ' · '.join(filter(None,(hunt_time,timer))))
        self.time_info.setText(timer)
        while not self.hunt_activity_results.empty():
            generation, stamp, observation = self.hunt_activity_results.get_nowait()
            if self.running and generation == self.generation:
                self.hunt_activity.update(generation, stamp, observation)
        self.activity_label.setText(self.hunt_activity.text(self.generation, now) if self.running
                                    else '사냥 신호 · 측정 대기')
        self.apply_jev_hints(now)
        self.apply_character_results(now)
        self.apply_buff_results(now)
        self.apply_meso_results(now)
        self.check_efficiency_change()
        self.apply_map_results()
        hunted = self.session.active_elapsed(now)
        window_key = (self.session.session_id, self.session.started is not None, int(hunted//10),
                      self.session.ended is not None, self.session.blocked)
        if window_key != self.window_display_key:
            self.window_display_key = window_key
            self.update_window_stats(now, hunted)
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
        last_sample_age = now-self.session.samples[-1][0] if self.session.samples else 0
        delayed = last_sample_age > 5
        if self.running and delayed and not self.session.blocked:
            if self.session.started is not None and round(last_sample_age, 3) > tracker.MAX_BOUNDARY_GAP:
                minutes, seconds = divmod(int(last_sample_age), 60)
                footer = f'기록 공백 {minutes}:{seconds:02d} · 마지막 값 표시'
            else:
                footer = f'인식 지연 · {int(last_sample_age)}초 전 값'
        if not self.session.blocked and (self.save_warning or self.audit_warning):
            footer = self.save_warning or self.audit_warning
        if not self.session.blocked and any(key[0] == self.session.session_id for key in self.capture_failures):
            footer = '시작 화면 저장 실패 · 기록 폴더/용량 확인'
        self.footer.setText('● '+footer)
        self.footer.setToolTip(' · '.join(filter(None,[self.session.stop_reason,self.save_warning,self.audit_warning]))
                               if self.session.blocked else warning)
        footer_style = 'color: '+('#e7b86e' if self.session.blocked or (self.running and delayed) else '#a8edbb' if self.running else '#7c8c9d')+';'
        if footer_style != self.footer.styleSheet():
            self.footer.setStyleSheet(footer_style)
        self.current_label.setText(self.value.get())
        starting_exp = self.session.rows[0][1] if self.session.rows else None
        self.baseline_label.setText(f'시작 기준 {starting_exp:,}' if starting_exp is not None else '시작 기준 —')
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
        if self.toggle_button.property('running') != self.running:
            self.toggle_button.setProperty('running', self.running)
            style = self.toggle_button.style()
            style.unpolish(self.toggle_button)
            style.polish(self.toggle_button)
        self.update_recent_button(now)
        self.auto_measure_tick(now)
        if self.preview_frame is not None and self.preview_frame[1] != self.preview_stamp:
            frame, self.preview_stamp = self.preview_frame
            frame = frame.convert('RGB')
            qimage = QImage(frame.tobytes(), frame.width, frame.height, frame.width*3, QImage.Format.Format_RGB888).copy()
            self.preview.setPixmap(QPixmap.fromImage(qimage).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        if self.meso_preview_frame is not None and self.meso_preview_frame[1] != self.meso_preview_stamp:
            frame, self.meso_preview_stamp = self.meso_preview_frame
            frame = frame.convert('RGB')
            qimage = QImage(frame.tobytes(), frame.width, frame.height, frame.width*3, QImage.Format.Format_RGB888).copy()
            self.meso_preview.setPixmap(QPixmap.fromImage(qimage).scaled(self.meso_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))

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
                'jev_enabled': self.jev_enabled, 'jev_full_screen': getattr(self, 'jev_full_screen', True),
                'auto_measure': self.auto_measure,
                'current_character': getattr(self, 'current_character', None),
                'nickname_patterns': getattr(self, 'nickname_patterns', {}),
                'cloud_remote': getattr(self, 'cloud_remote', None),
                'meso_trade_threshold': getattr(self, 'meso_trade_threshold', meso.TRADE_THRESHOLD),
                'meso_region': getattr(self, 'meso_region', None),
            }), encoding='utf-8')
            temporary.replace(tracker.BASE/'settings.json')
        except OSError as exc:
            tracker.log_error('설정 저장', exc); self.status.set('창 위치/영역 저장 실패')

    def close(self): self.window.close()

    def shutdown(self):
        self.key_repeat.shutdown()
        if not self.finish_measurement('앱 종료'): return False
        self.save_config(); self.cancel_selection(); self.stop.set(); self.timer.stop()
        self._shutdown_probes()
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
    icon = tracker.RESOURCES/'planet-helper-icon.png'
    if sys.platform.startswith('linux') and icon.is_file():
        application.setWindowIcon(QIcon(str(icon)))
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
                controller._shutdown_probes()
                controller.start_captures.close()
        lock.close()
        if diagnostic_data is not None:
            diagnostic_data.cleanup()
