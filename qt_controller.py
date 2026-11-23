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
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QTimer, QObject, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, QShortcut, QKeySequence, QIcon
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QFileDialog, QMessageBox, QMenu, QMenuBar, QDialog, QComboBox)
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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(13, 23, 36, round(self.controller.window_opacity*255)))
        painter.setPen(QPen(QColor('#35414d'), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


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
    APP_TITLE = f'플래닛 도우미 {tracker.APP_VERSION}'
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
        self.jev_arbiter = jev.Arbiter(lambda: self.jev_enabled)
        self.jev_frames = jev_assist.FrameRing()
        self.jev_hud_missing = None
        self.jev_stall_saved = None
        self.meso_reader = meso.Reader(self.jev_arbiter)
        self.meso_capture_lock = threading.Lock()
        self.meso_results = queue.SimpleQueue()
        self.meso_confirmation = meso.Confirmation()
        self.meso_rechecking = False
        self.meso_log = meso.DailyLog()
        self.meso_source = None
        self.meso_opening = 0
        self.meso_visible = False
        self.meso_reader_source = None
        self.meso_last_flush = float('-inf')
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
        self.current_character = config.get('current_character')
        self.nickname_patterns = config.get('nickname_patterns', {})
        self.meso_trade_threshold = config.get('meso_trade_threshold', meso.TRADE_THRESHOLD)
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
        from display_panel import resize
        resize(self)

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

    def export_records(self):
        import records_backup
        from datetime import datetime
        suggested = str(Path.home()/f"planet-helper-records-{datetime.now():%Y%m%d}.zip")
        path, _ = QFileDialog.getSaveFileName(self.window, '사냥 기록 내보내기', suggested, '백업 (*.zip)')
        if not path: return
        if not path.endswith('.zip'): path += '.zip'
        try:
            count = records_backup.export_zip(path)
        except OSError as exc:
            tracker.log_error('기록 내보내기', exc)
            QMessageBox.warning(self.window, '사냥 기록 내보내기', f'내보내지 못했어요.\n{exc}'); return
        QMessageBox.information(self.window, '사냥 기록 내보내기',
                                f'기록 {count}개를 저장했어요.\n{path}\n\n'
                                '다른 컴퓨터의 플래닛 도우미에서 "백업 파일에서 가져오기"로 합칠 수 있어요.')

    def import_records(self):
        import records_backup, zipfile
        path, _ = QFileDialog.getOpenFileName(self.window, '사냥 기록 가져오기', str(Path.home()), '백업 (*.zip)')
        if not path: return
        try:
            added, skipped, names = records_backup.import_zip(path)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            tracker.log_error('기록 가져오기', exc)
            QMessageBox.warning(self.window, '사냥 기록 가져오기', f'가져오지 못했어요.\n{exc}'); return
        QMessageBox.information(self.window, '사냥 기록 가져오기',
                                f'새 기록 {added}개를 추가했어요.\n'
                                f'이미 있는 기록 {skipped}개는 그대로 두었어요.\n'
                                f'사냥터·캐릭터 이름 {names}개를 합쳤어요.')

    def open_backups(self):
        folder = tracker.BASE/'records-backups'
        folder.mkdir(parents=True, exist_ok=True)
        self.open_path(folder)

    def run_auto_backup(self):
        """Daily copy of the records, in the background so startup stays quick."""
        def work():
            try:
                import records_backup
                records_backup.auto_backup()
            except Exception as exc:
                tracker.log_error('자동 백업', exc)
            if self.cloud_remote:
                self.cloud_work(push_only=False, manual=False)
        threading.Thread(target=work, name='records-backup', daemon=True).start()

    def cloud_settings(self):
        from PySide6.QtWidgets import QInputDialog
        import cloud_sync
        if cloud_sync.rclone() is None:
            QMessageBox.information(self.window, '구글 드라이브 동기화',
                'rclone이 필요해요.\n\n리눅스: 패키지 관리자에서 rclone 설치\n맥: brew install rclone\n\n'
                '설치한 뒤 다시 열어 주세요.')
            return
        remotes = cloud_sync.remotes()
        if not remotes:
            QMessageBox.information(self.window, '구글 드라이브 동기화',
                '먼저 rclone에 구글 드라이브를 한 번 연결해야 해요. 터미널에서 실행하세요:\n\n'
                '    rclone config create gdrive drive scope=drive.file\n\n'
                '브라우저가 열리면 구글 계정으로 로그인하고 허용을 누르면 돼요.\n'
                '(drive.file 권한: 이 앱이 올린 파일만 볼 수 있어요.)\n\n'
                '끝나면 이 메뉴를 다시 열어 주세요.')
            return
        off = '동기화 끄기'
        current = (self.cloud_remote or '').split(':', 1)[0] + ':' if self.cloud_remote else None
        options = ([current] if current in remotes else []) + [r for r in remotes if r != current] + [off]
        choice, ok = QInputDialog.getItem(
            self.window, '구글 드라이브 동기화',
            '사용할 rclone 연결 (그 안의 planet-helper 폴더에 컴퓨터별 백업을 올려요)', options, 0, False)
        if not ok: return
        if choice == off:
            self.cloud_remote = None
            self.save_config(); self.status.set('구글 드라이브 동기화 꺼짐'); return
        self.cloud_remote = choice + 'planet-helper'
        self.save_config()
        self.start_cloud_sync(manual=True)

    def start_cloud_sync(self, manual=False, push_only=False):
        if not self.cloud_remote: return
        if manual: self.status.set('구글 드라이브 동기화 중…')
        threading.Thread(target=self.cloud_work, args=(push_only, manual),
                         name='cloud-sync', daemon=True).start()

    def cloud_work(self, push_only, manual):
        """Runs off the UI thread; reports through a queued signal."""
        import cloud_sync
        remote = self.cloud_remote
        if not remote: return
        if not self.cloud_lock.acquire(blocking=False):
            if manual: self.cloud_signals.done.emit('이미 동기화 중이에요')
            return
        try:
            if push_only:
                cloud_sync.push(remote)
                text = '구글 드라이브에 기록 올림'
            else:
                text = '구글 드라이브 ' + cloud_sync.describe(cloud_sync.sync(remote))
        except Exception as exc:
            tracker.log_error('구글 드라이브 동기화', exc)
            try:
                cloud_sync.save_state(dict(time=time.time(), ok=False, error=str(exc)[:120]))
            except OSError:
                pass
            text = '구글 드라이브 동기화 실패 · last-error.log 확인'
        finally:
            self.cloud_lock.release()
        if manual or not push_only:
            self.cloud_signals.done.emit(text)

    def cloud_finished(self, text):
        self.status.set(text)

    def show(self):
        if not getattr(self, 'auto_backup_started', False):
            self.auto_backup_started = True
            QTimer.singleShot(5000, self.run_auto_backup)
        self.window.show()
        self.native_overlay(self.window, 3 if self.always_on_top else 0)
        self.window.raise_()
        self.window.activateWindow()

    def context_menu(self, point):
        menu = self.build_context_menu()
        try:
            menu.exec(self.window.mapToGlobal(point))
        finally:
            menu.deleteLater()

    def build_context_menu(self):
        menu = QMenu(self.window)
        menu.addAction('사냥 기록 비교', self.show_history)
        menu.addAction('이번 사냥터 메모', self.edit_memo)
        menu.addSeparator()

        def add_toggle(parent, title, checked, callback):
            action = parent.addAction(title)
            action.setCheckable(True)
            action.setChecked(checked)
            action.toggled.connect(callback)

        measurement = menu.addMenu('측정·화면')
        add_toggle(measurement, '항상 맨 위', self.always_on_top, self.toggle_pin)
        measurement.addAction('배경 투명도', self.opacity_settings)
        measurement.addSeparator()
        add_toggle(measurement, '경험치 증가 시 자동 측정', self.auto_measure, self.set_auto_measure)
        add_toggle(measurement, '대기 중 최근 경험치 모으기', self.recent_recording, self.set_recent_recording)
        add_toggle(measurement, '플래닛 버닝 적용', self.burning, self.set_burning)
        measurement.addAction('연타 설정', self.repeat_settings)

        recognition = menu.addMenu('게임 인식')
        recognition.addAction(f"현재 캐릭터: {self.current_character or '지정 안 함'}…", self.choose_character)
        recognition.addAction('현재 닉네임 등록·수정…', self.register_nickname)
        recognition.addSeparator()
        recognition.addAction('경험치 영역 지정…', self.select)
        recognition.addAction('JEV API 키 설정', self.api_key_settings)

        gains = menu.addMenu('메소·버프')
        gains.addAction('메소 큰 변동 기준 금액', self.meso_threshold_settings)
        gains.addAction('이번 측정의 큰 메소 변동 분류…', self.classify_meso_change)
        gains.addSeparator()
        for kind, label in exp_buffs.LABELS.items():
            gains.addAction(f'{label} 아이콘 등록…',
                            lambda checked=False, key=kind: self.register_buff_icon(key))

        records = menu.addMenu('기록·백업')
        records.addAction('오늘·이번 주 요약', self.show_period_summary)
        records.addAction('CSV 저장…', self.export)
        records.addAction('기록 폴더 열기', self.open_records)
        records.addSeparator()
        records.addAction('백업 파일로 내보내기…', self.export_records)
        records.addAction('백업 파일에서 가져오기 (합치기)…', self.import_records)
        records.addAction('자동 백업 폴더 열기', self.open_backups)
        records.addSeparator()
        records.addAction('구글 드라이브 동기화 설정…', self.cloud_settings)
        if self.cloud_remote:
            records.addAction('지금 동기화', lambda: self.start_cloud_sync(manual=True))
            import cloud_sync
            info = records.addAction(cloud_sync.describe(cloud_sync.load_state()))
            info.setEnabled(False)

        help_menu = menu.addMenu('도움말·권한')
        help_menu.addAction('인식 정보', self.show_information)
        help_menu.addAction('시작 화면 폴더 열기', self.open_start_captures)
        self.extra_menu_actions(help_menu)

        menu.addSeparator()
        menu.addAction('종료', self.close)
        return menu

    def register_buff_icon(self, kind):
        label = exp_buffs.LABELS[kind]
        path, _ = QFileDialog.getOpenFileName(
            self.window, f'{label} 아이콘 한 칸을 자른 이미지 선택', '', '이미지 (*.png *.jpg *.jpeg *.webp)')
        if not path:
            return
        try:
            exp_buffs.register(tracker.BASE, kind, path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self.window, '버프 아이콘 등록', str(exc))
            return
        self.buff_has_templates = True
        self.buff_revision += 1
        self.buff_confirmation = exp_buffs.Confirmation()
        self.buff_label.setText(f'{label} 등록 완료 · 측정 중 확인')

    def register_nickname(self):
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        sample = self.nickname_sample
        if sample is None or not sample[2] or sample[0] != self.character_source or time.monotonic() - sample[1] > 15:
            self.status.set('자동 인식으로 게임 HUD가 표시되면 닉네임을 등록할 수 있어요')
            return
        source, _, key = sample
        text, ok = QInputDialog.getText(
            self.window, '현재 닉네임 등록·수정', '게임 HUD에 보이는 닉네임을 입력해 주세요.',
            text=self.nickname_patterns.get(key) or self.character_identity[1] or self.current_character or '')
        if not ok:
            return
        name = text.strip()
        if not character_hud.valid_nickname(name):
            QMessageBox.warning(self.window, '닉네임 확인', '한글·영문·숫자 2~16자를 입력해 주세요.')
            return
        self.nickname_patterns = {**self.nickname_patterns, key: name}
        self.save_config()
        if self.nickname_sample and self.nickname_sample[0] == source and self.nickname_sample[2] == key:
            self.character_candidate = (self.character_candidate[0], name)
            self.set_detected_character((self.character_identity[0], name))

    def meso_threshold_settings(self):
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        text, ok = QInputDialog.getText(
            self.window, '메소 큰 변동 기준',
            '잔액 차이가 이 금액을 넘으면 큰 변동으로 표시합니다.',
            text=f'{self.meso_trade_threshold:,}')
        if not ok:
            return
        try:
            value = int(text.strip().replace(',', ''))
            if not meso.MIN_THRESHOLD <= value <= meso.MAX_THRESHOLD:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self.window, '메소 큰 변동 기준', '1~1조 사이의 정수를 입력해 주세요.')
            return
        self.meso_trade_threshold = value
        self.save_config()

    def classify_meso_change(self):
        from PySide6.QtWidgets import QInputDialog
        balance = self.session.meso
        if not self.running or not balance.large_changes:
            self.status.set('이번 측정에서 분류할 큰 메소 변동이 없어요')
            return
        options = [f'{i + 1}. {amount:+,} · ' + ('거래·구매' if i in balance.marked else '미분류')
                   for i, amount in enumerate(balance.large_changes)]
        choice, ok = QInputDialog.getItem(
            self.window, '메소 큰 변동 분류', '거래·구매 표시를 켜거나 끌 변동을 고르세요.',
            options, 0, False)
        if ok and self.session.meso is balance and self.running:
            balance.toggle_trade(options.index(choice))
            self.session.meso_dirty = True

    def meso_character(self):
        if self.session.character_conflict:
            return None
        return self.session.character_name or self.current_character or ''

    def clear_meso_confirmation(self, source):
        self.meso_source = source
        self.meso_visible = False
        self.meso_confirmation = meso.Confirmation()
        self.meso_rechecking = False

    def apply_meso_results(self, now, *, finishing=False):
        session = self.session
        character = self.meso_character()
        pending_start = session.meso_start_frame
        if pending_start is not None:
            stamp, frame, window = pending_start
            if finishing or not self.running or now - stamp > meso.END_WINDOW:
                session.meso_start_frame = None
            elif self.queue_meso_frame(self.generation, stamp, frame, window):
                session.meso_start_frame = None
        if self.meso_source is not None and self.meso_source[:2] != (self.generation, character):
            self.clear_meso_confirmation(None)
        if session.meso.source is not None and session.meso.source[:2] != (self.generation, character):
            session.meso.check_source(None)
        while not self.meso_results.empty():
            generation, window, stamp, observed_character, value = self.meso_results.get_nowait()
            if (not self.running or generation != self.generation or character is None
                    or observed_character != character or window is None or session.meso_start_at is None
                    or stamp < session.meso_start_at or stamp < now - meso.END_WINDOW):
                continue
            source = generation, character, window
            session.meso.check_source(source)
            if source != self.meso_source:
                self.clear_meso_confirmation(source)
            if value is meso.RECHECK:
                self.meso_rechecking = True
                self.meso_confirmation.observe(None, stamp)
                continue
            if value is None:
                self.meso_visible = False
            elif not self.meso_visible:
                self.meso_opening += 1
                self.meso_visible = True
            confirmed = self.meso_confirmation.observe(value, stamp)
            if confirmed is None:
                if value is not None and session.meso.end is not None:
                    self.meso_rechecking = True
                continue
            self.meso_rechecking = False
            try:
                self.meso_log.record(tracker.BASE, character, confirmed, datetime.now())
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
            session.meso_dirty |= session.meso.sample(
                confirmed, source, stamp, self.meso_opening, session.meso_start_at,
                self.meso_trade_threshold, first_seen=self.meso_confirmation.first_seen)
        if (not finishing and self.running and session.meso_dirty and session.rows and not session.ledger_dirty
                and now - self.meso_last_flush >= 10):
            self.meso_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
        text = self.meso_text()
        if text != self.meso_label.text():
            self.meso_label.setText(text)
        ready = self.meso_status(now)
        if self.meso_status_label.text() != ready:
            self.meso_status_label.setText(ready)
        delta = session.meso.hunted()
        if (self.running and not session.meso.source_changed
                and session.meso.source == self.meso_source):
            delta = session.meso.observed_delta()
        amount = '—' if delta is None else f'{delta:+,}'
        if self.meso_delta_label.text() != amount:
            self.meso_delta_label.setText(amount)

    def meso_status(self, now):
        balance = self.session.meso
        if balance.source_changed:
            return '시작·종료 잔액 확인 불가 · 캐릭터 또는 게임 창 변경'
        start = '시작 잔액 확인' if balance.start is not None else '시작 잔액 대기'
        if (self.running and balance.start is None and self.session.meso_start_at is not None
                and now - self.session.meso_start_at > meso.START_WINDOW):
            start = '시작 잔액 확인 시간 지남'
        end = balance.complete
        if self.running:
            end = not self.meso_rechecking and balance.end_ready(self.meso_source, now)
        return start + (' · 종료 잔액 확인' if end else ' · 종료 잔액 대기')

    def meso_text(self):
        current = self.meso_confirmation.current
        if self.running and self.meso_rechecking:
            if self.session.meso.end is not None:
                current = self.session.meso.end
            return ('메소' if current is None else f'메소 {current:,}') + ' · 잔액 재확인 중'
        if current is None:
            return '메소 · 측정 중 인벤토리를 열면 기록'
        text = f'메소 {current:,}'
        balance = self.session.meso
        if self.running:
            if balance.source_changed:
                return text + ' · 캐릭터 또는 게임 창 변경 · 이번 잔액 차이 확인 안 됨'
            if balance.readings < 2 or balance.source != self.meso_source:
                if balance.start is None:
                    return text + ' · 시작 후 30초 안의 잔액 필요'
                return text + ' · 종료 직전에 인벤토리를 다시 열어 주세요'
            if balance.large_changes:
                text += f' · 큰 변동 {len(balance.large_changes)}건'
            excluded = balance.excluded_trade()
            if excluded:
                text += f' · 거래·구매 {excluded:+,} 제외'
            return text
        today = self.meso_log.change(self.meso_character())
        return text + (f' · 오늘 {today:+,}' if today is not None else '')

    def show_history(self):
        from record_history import build_dialog
        self.history_dialog = build_dialog(self)
        self.history_dialog.show(); self.history_dialog.raise_()

    def choose_character(self):
        """New records carry this name unless OCR reads one or the record is edited later."""
        from PySide6.QtWidgets import QInputDialog
        import record_history
        known = record_history.known_characters()
        current = self.current_character or ''
        options = [current] + [name for name in known if name != current] if current else ([''] + known)
        text, ok = QInputDialog.getItem(
            self.window, '현재 캐릭터',
            '지금 사냥하는 캐릭터 (새 기록에 붙어요 · 비우면 지정 해제)', options, 0, True)
        if not ok: return
        name = ' '.join(text.split())[:24] or None
        self.current_character = name
        self.save_config()
        if self.session.rows:
            try:
                self.save_summary()  # the record being measured gets the name too
            except OSError as exc:
                tracker.log_error('현재 캐릭터 저장', exc)
        self.status.set(f'현재 캐릭터 · {name}' if name else '현재 캐릭터 지정 해제')

    def show_period_summary(self):
        import record_history
        try:
            text = record_history.period_text(record_history.collect())
        except Exception as exc:
            tracker.log_error('사냥 요약', exc)
            text = '요약을 만들지 못했어요. last-error.log를 확인해 주세요.'
        QMessageBox.information(self.window, '오늘·이번 주 요약', text)

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
            'OCR이 헷갈리거나 상황 판단이 필요할 때만 게임 화면 일부를 JEV에 보내 확인\n'
            '사냥터 이름 · 메소/경험치 판독 · 닉네임 · 버프 · 경험치가 멈춘 이유 · 효율 변화 태그 · 화면 상태\n'
            '우클릭 메뉴에서 API 키 설정'
            if self.jev_enabled else 'JEV를 켜면 판독이 헷갈릴 때와 상황 판단에 JEV를 사용 (게임 화면 일부를 전송)')

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
        self.source_label.setText('MapleStory Worlds · 게임 창 캡처')
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
            self.session.meso.finish(None if self.meso_rechecking else self.meso_source, now)
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
        meso_probe = (not recent and models_available and session.meso_start_at is not None
                      and session.meso_start_frame is None and not self.meso_capture_lock.locked()
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
                waiting_for_start = (session.meso.start is None
                                     and stamp - session.meso_start_at <= meso.START_WINDOW)
                interval = 1.0 if waiting_for_start else hunt_activity.SCAN_INTERVAL
                self.capture_clients.next_meso_scan = stamp + interval
        if probe:
            self.capture_clients.next_activity_scan = stamp + hunt_activity.SCAN_INTERVAL
            if full is not None and self.hunt_activity_lock.acquire(blocking=False):
                try:
                    level_roi = image.info.get('level_region')
                    style = image.info.get('ui_kind')
                    if not self.character_read_needed(generation, window, stamp):
                        level_roi = None
                    threading.Thread(target=self.read_hunt_activity,
                                     args=(generation, observed_at, full, window, level_roi, style, recent),
                                     name='hunt-activity', daemon=True).start()
                except RuntimeError as exc:
                    self.hunt_activity_lock.release()
                    tracker.log_error('사냥 신호 판독 시작', exc)
        if not starting:
            image.info.pop('full_game_image', None)
        return image

    def start_meso_capture(self, generation, stamp, details):
        full = details.get('full_game_image')
        window = details.get('window_id')
        if full is not None and window is not None and map_name.available():
            if not self.queue_meso_frame(generation, stamp, full, window):
                self.session.meso_start_frame = stamp, full, window
        self.session.meso_start_at = stamp

    def queue_meso_frame(self, generation, stamp, frame, window):
        if not self.meso_capture_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self.read_meso_frame,
                             args=(generation, stamp, frame, window, self.meso_character()),
                             name='meso-reader', daemon=True).start()
        except RuntimeError as exc:
            self.meso_capture_lock.release()
            tracker.log_error('메소 판독 시작', exc)
            return False
        return True

    def read_meso_frame(self, generation, stamp, frame, window, character):
        try:
            if generation == self.generation:
                source = generation, window
                if self.meso_reader_source != source:
                    self.meso_reader = meso.Reader(self.jev_arbiter)
                    self.meso_reader_source = source
                value = self.meso_reader.read(frame, stamp)
                if self.meso_reader.needs_recheck:
                    value = meso.RECHECK
                self.meso_results.put((generation, window, stamp, character, value))
        except Exception as exc:
            tracker.log_error('메소 판독', exc)
        finally:
            self.meso_capture_lock.release()

    def read_hunt_activity(self, generation, stamp, frame, window, level_roi, style, recent=False):
        try:
            try:
                if level_roi is not None and window is not None and map_name.available():
                    job, nickname, key = character_hud.inspect(frame, level_roi, style, self.nickname_patterns)
                    if nickname is None and key is not None:
                        nickname = self.jev_nickname(frame, level_roi, style, key)
                    self.character_results.put((generation, window, stamp, job, nickname, key))
            except Exception as exc:
                tracker.log_error('캐릭터 HUD 판독', exc)
            if recent:
                return
            self.jev_frames.remember(stamp, frame)
            try:
                if window is not None and self.buff_has_templates:
                    revision = self.buff_revision
                    readings = self.jev_buff_readings(frame, stamp, self.buff_detector.read(frame, tracker.BASE))
                    self.buff_results.put((generation, window, revision, stamp, readings))
            except Exception as exc:
                tracker.log_error('경험치 버프 판독', exc)
            try:
                if map_name.available():
                    observation = hunt_activity.read(frame, self.hunt_activity_cache)
                    self.hunt_activity_results.put((generation, stamp, observation))
            except Exception as exc:
                tracker.log_error('사냥 신호 판독', exc)
        finally:
            self.hunt_activity_lock.release()

    CHARACTER_RECHECK_SECONDS = 8.0

    def character_read_needed(self, generation, window, now):
        return (generation, window) != self.character_source or now >= self.character_recheck_at

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
            self.jev_button.setAccessibleDescription(text)

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

    # ---- JEV: ask once per situation; keep the local result whenever JEV is unsure ----

    def jev_nickname(self, frame, level_roi, style, key):
        """Worker thread: a known character name JEV recognised in the nickname line, or None."""
        found, name = self.jev_arbiter.answer(('nick', key))
        if found:
            return name
        crops = character_hud.lines(frame, level_roi, style)
        if crops is not None:
            def question():
                import record_history
                names = [*([self.current_character] if self.current_character else []),
                         *self.nickname_patterns.values(), *record_history.known_characters()]
                return jev_assist.ask_nickname(crops[1], names)
            self.jev_arbiter.submit(('nick', key), question)
        return None

    def jev_buff_readings(self, frame, stamp, readings):
        """Worker thread: settle 'unknown' buff readings with one JEV look per kind and period."""
        readings = dict(readings)
        bucket = int(stamp // jev_assist.BUFF_SECONDS)
        for kind, value in readings.items():
            if value != 'unknown':
                continue
            question = ('buff', kind, bucket)
            found, verdict = self.jev_arbiter.answer(question)
            if found:
                if verdict:
                    readings[kind] = verdict
                continue
            corner = exp_buffs.buff_corner(frame)

            def ask(kind=kind, corner=corner):
                from PIL import Image
                with Image.open(exp_buffs.template_path(tracker.BASE, kind)) as icon:
                    return jev_assist.ask_buff(icon.convert('RGB'), corner)
            self.jev_arbiter.submit(question, ask)
        return readings

    def note_missing_hud(self, generation, full):
        """Worker thread: once the HUD stays missing, ask (at most once a minute) what the window shows."""
        now = time.monotonic()
        if not self.running or self.session.first is not None:
            self.jev_hud_missing = None
            return
        missing = self.jev_hud_missing
        if missing is None or missing[0] != generation:
            self.jev_hud_missing = (generation, now, None)
            return
        if full is None or now - missing[1] < jev_assist.SCREEN_SECONDS:
            return
        question = ('screen', generation, int(now // 60))
        if self.jev_arbiter.submit(question, lambda: jev_assist.ask_screen(full)):
            self.jev_hud_missing = (generation, missing[1], question)

    def change_suggestion(self, change):
        """JEV's tag for an efficiency change, from frames before and after it, or None."""
        if not self.jev_enabled or self.session.started is None:
            return None
        question = ('change', self.session.session_id, change['minute'])
        found, tag = self.jev_arbiter.answer(question)
        if found:
            return tag
        before = self.jev_frames.before(self.session.started + (change['minute']-1)*60)
        after = self.jev_frames.latest(since=self.session.started + change['minute']*60)
        if before is not None and after is not None:
            self.jev_arbiter.submit(question, lambda: jev_assist.ask_change(before, after))
        return None

    def apply_jev_hints(self, now):
        """Show JEV answers for the current measurement and start the questions that are due."""
        session = self.session
        enabled = self.jev_enabled and self.running
        # What the window shows while the level and EXP display cannot be found.
        missing = self.jev_hud_missing
        hint = ''
        if enabled and missing and missing[0] == self.generation and missing[2] and session.first is None:
            hint = self.jev_arbiter.answer(missing[2])[1] or ''
        if hint:
            self.set_jev_status(f'JEV 화면 확인 · {hint}')
        elif self.jev_status.text().startswith('JEV 화면 확인'):
            self.set_jev_status('')
        # Why EXP stopped: asked once per stall, saved with the record.
        activity = self.hunt_activity
        if (enabled and session.started is not None and activity.generation == self.generation
                and activity.gained > float('-inf') and now - activity.gained >= jev_assist.STALL_SECONDS):
            question = ('stall', session.session_id, activity.gained)
            found, reason = self.jev_arbiter.answer(question)
            if not found:
                frame = self.jev_frames.latest(since=activity.gained + 30)
                if frame is not None:
                    self.jev_arbiter.submit(question, lambda: jev_assist.ask_stall(frame))
            elif reason:
                suffix = f' · JEV: {reason}'
                if not self.activity_label.text().endswith(suffix):
                    self.activity_label.setText(self.activity_label.text() + suffix)
                if self.jev_stall_saved != question:
                    self.jev_stall_saved = question
                    minute = int(max(0.0, activity.gained - session.started) // 60)
                    try:
                        jev_assist.save_stall(session.session_id, minute, reason)
                    except OSError as exc:
                        tracker.log_error('멈춘 이유 저장', exc)
        # Suggested tag for an efficiency change.
        change = getattr(self, 'pending_change', None)
        if change is not None and enabled:
            suggestion = self.change_suggestion(change)
            text = self.change_prompt_text + (f' · JEV 추천: {suggestion}' if suggestion else '')
            if self.change_prompt.text() != text:
                self.change_prompt.setText(text)

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
            self.change_prompt_text = (
                f"{direction} {pending['minute']}분째부터 분당 {short_exp(pending['before'])} → {short_exp(pending['after'])}"
                ' · 뭐가 달라졌나요? (눌러서 태그)')
            self.change_prompt.setText(self.change_prompt_text)
            if self.change_prompt.isHidden():
                self.change_prompt.setVisible(True)
                from display_panel import resize
                resize(self)

    def answer_change_prompt(self):
        change = getattr(self, 'pending_change', None)
        if change is None: return
        from PySide6.QtWidgets import QInputDialog
        import segments
        suggestion = self.change_suggestion(change)
        text, ok = QInputDialog.getText(
            self.window, '효율 변화',
            f"{change['minute']}분째부터 분당 {change['before']:,.0f} → {change['after']:,.0f}\n"
            '여기서 뭐가 달라졌나요? (예: 버프 끝, 파티 합류, 맵 이동)\n비워 두고 확인하면 다시 묻지 않아요.'
            + ('\nJEV가 화면을 비교해 추천한 태그를 미리 넣어 뒀어요.' if suggestion else ''),
            text=suggestion or '')
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

    def set_character_source(self, source):
        if source != self.character_source:
            self.character_source = source
            self.character_candidate = (None, None)
            self.character_identity = (None, None)
            self.character_recheck_at = 0.0
            self.nickname_sample = None
            self.update_capture_label()

    def update_capture_label(self):
        if not self.auto_hud:
            self.source_label.setText('MapleStory Worlds · 수동 영역')
            return
        job, nickname = self.character_identity
        fields = [f'Lv. {self.session.level}'] if self.running else []
        fields.append(f'접속 닉네임 {nickname}' if nickname else '접속 닉네임 확인 대기')
        if job:
            fields.append(job)
        self.source_label.setText(' · '.join(fields))

    def set_detected_character(self, identity):
        self.character_identity = identity
        self.update_capture_label()
        nickname = identity[1]
        session = self.session
        if self.running and nickname and not session.character_conflict and nickname != session.character_name:
            session.character_conflict = session.character_name is not None
            session.character_name = None if session.character_conflict else nickname
            if session.rows:
                try:
                    self.save_summary()
                except OSError as exc:
                    tracker.log_error('사냥 기록 캐릭터 저장', exc)

    def apply_character_results(self, now):
        while not self.character_results.empty():
            generation, window, stamp, job, nickname, key = self.character_results.get_nowait()
            source = generation, window
            if source != self.character_source or generation != self.generation or now - stamp > 15:
                continue
            previous = self.nickname_sample
            if previous and stamp <= previous[1]:
                continue
            self.nickname_sample = (source, stamp, key) if key else None
            old_job, old_name = self.character_candidate
            registered = self.nickname_patterns.get(key)
            confirmed_name = registered or (nickname if nickname and nickname == old_name
                                            and previous and previous[2] == key else None)
            self.character_candidate = (job, nickname)
            self.set_detected_character((job if job == old_job else None, confirmed_name))
            self.character_recheck_at = now + (self.CHARACTER_RECHECK_SECONDS if confirmed_name else 2.0)
        if self.nickname_sample and now - self.nickname_sample[1] > 15:
            self.set_character_source(None)

    def apply_buff_results(self, now):
        session = self.session
        while not self.buff_results.empty():
            generation, window, revision, stamp, readings = self.buff_results.get_nowait()
            if (not self.running or (generation, window) != self.buff_window
                    or generation != self.generation or revision != self.buff_revision):
                continue
            start = self.buff_confirmation.observe((generation, window), stamp, readings)
            if session.started is not None and start is not None:
                session.exp_buffs_dirty |= exp_buffs.record(
                    session.exp_buff_intervals, self.buff_confirmation.current,
                    start - session.started, stamp - session.started)
        if (self.running and session.exp_buffs_dirty and session.rows and not session.ledger_dirty
                and now - self.buff_last_flush >= 10):
            self.buff_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('경험치 버프 구간 저장', exc)
        if not self.buff_has_templates:
            text = '경험치 버프 · 아이콘 미등록'
        elif self.running:
            text = self.buff_confirmation.text(self.generation, now)
        else:
            text = '경험치 버프 · 측정 대기'
        self.buff_label.setText(text)

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
                'jev_enabled': self.jev_enabled, 'auto_measure': self.auto_measure,
                'current_character': getattr(self, 'current_character', None),
                'nickname_patterns': getattr(self, 'nickname_patterns', {}),
                'cloud_remote': getattr(self, 'cloud_remote', None),
                'meso_trade_threshold': getattr(self, 'meso_trade_threshold', meso.TRADE_THRESHOLD),
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
                controller.start_captures.close()
        lock.close()
        if diagnostic_data is not None:
            diagnostic_data.cleanup()
