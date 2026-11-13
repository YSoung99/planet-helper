"""Linux hold-to-repeat adapter, reusing the installed evdev relay engine."""
import fcntl
import json
import os
import random
import time
from pathlib import Path
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from key_repeat import validated_settings, DEFAULT_SETTINGS
from linux_repeat_core import Repeater, Keyboards, GameFocus, UInput, E, DEVICE_NAME


class ConfiguredRepeater(Repeater):
    def __init__(self, output, focus, settings, clock=time.monotonic):
        super().__init__(output, focus, clock)
        self.settings = settings

    def press(self, source, blocked=False, code=E.KEY_A):
        fresh = code in self.states and not self.states[code].held
        accepted = super().press(source, blocked, code)
        if accepted and fresh:
            state = self.states[code]
            # First press remains a normal held key until the configured delay.
            if not state.down: self.key(code, 1)
            state.initial_press = True
            state.release_at = self.clock()+self.settings['firstDelayMs']/1000
        return accepted

    def tick(self):
        if not self.running: return
        if not self.enabled or not self.focus():
            self.stop(); return
        now = self.clock()
        for code, state in self.states.items():
            if not state.held: continue
            if state.initial_press:
                if now >= state.release_at:
                    self.key(code, 0); state.initial_press = False
                    state.next_press = now+.030
                continue
            if state.down and now >= state.release_at: self.key(code, 0)
            if not state.down and now >= state.next_press:
                self.key(code, 1); state.release_at = now+.030
                state.next_press = now+random.randint(self.settings['minIntervalMs'],self.settings['maxIntervalMs'])/1000


class ConfiguredKeyboards(Keyboards):
    def handle(self, item, event):
        if self.engine.settings['attackTriggerKeyCode'] != 6:
            return super().handle(item,event)
        # Z is the attack trigger; physical A passes through unchanged.
        if event.type == E.EV_KEY and event.code == E.KEY_A:
            return item.forward(event)
        if event.type == E.EV_KEY and event.code == E.KEY_Z:
            if event.value == 0 and E.KEY_Z in item.suppressed:
                item.suppressed.discard(E.KEY_Z)
                self.engine.release(item.device.path, code=E.KEY_A); return
            if E.KEY_Z in item.suppressed: return
            if event.value == 1 and self.engine.press(item.device.path,self.modifiers_held(),code=E.KEY_A):
                item.suppressed.add(E.KEY_Z); return
            return item.forward(event)
        return super().handle(item,event)


class KeyRepeatBridge(QObject):
    changed = Signal(dict)

    def __init__(self, data_path, helper_path=None, parent=None):
        super().__init__(parent)
        self.settings_path = Path(data_path)/'key-repeat.json'
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings_error = ''
        try:
            if self.settings_path.exists(): self.settings = validated_settings(json.loads(self.settings_path.read_text()))
        except (OSError,ValueError,TypeError) as exc: self.settings_error = str(exc)
        self.closing = False
        self.engine = self.keyboards = self.output = self.focus = self.lock = None
        self.state = dict(enabled=False,active=False,accessibility=True,inputMonitoring=True,
                          legacyRunning=False,status='연타 꺼짐',error='')
        self.timer = QTimer(self); self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(5); self.timer.timeout.connect(self.poll)
        self.last_state = None

    def start(self):
        self.publish()

    def publish(self):
        if self.engine:
            self.state.update(enabled=self.engine.enabled,active=self.engine.running,
                status=self.engine.message if self.keyboards.ready else '입력 장치 연결 대기',error='')
        if self.state != self.last_state:
            self.last_state = dict(self.state); self.changed.emit(dict(self.state))

    def set_enabled(self, enabled):
        if self.closing: return
        if not enabled:
            self.release(); self.state.update(enabled=False,active=False,status='연타 꺼짐',error='',legacyRunning=False)
            self.publish(); return
        if self.engine: return
        try:
            runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp'))
            self.lock = (runtime/'maple-a-repeat.lock').open('a+')
            try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.release()
                self.state.update(legacyRunning=True,status='기존 키 반복 앱을 종료한 뒤 켜 주세요',error='')
                self.publish(); return
            self.focus = GameFocus()
            self.output = UInput({E.EV_KEY:list(range(E.KEY_ESC,E.KEY_F24+1))},name=DEVICE_NAME,phys='planet-helper/input0')
            self.engine = ConfiguredRepeater(self.output,self.focus.is_game,self.settings)
            self.keyboards = ConfiguredKeyboards(self.engine)
            if not self.keyboards.devices: raise RuntimeError('사용 가능한 키보드 없음 · 입력 장치 권한과 Toshy 확인')
            self.next_scan = time.monotonic()+2
            self.state.update(legacyRunning=False,error='')
            self.timer.start(); self.publish()
        except Exception as exc:
            self.release(); self.state.update(enabled=False,active=False,error=str(exc),status='입력 연결 실패')
            self.publish()

    def poll(self):
        try:
            self.keyboards.process(); self.engine.tick()
            if time.monotonic() >= self.next_scan:
                self.keyboards.refresh(); self.next_scan = time.monotonic()+2
            if not self.engine.enabled:
                self.set_enabled(False); return
        except Exception as exc:
            self.release(); self.state.update(enabled=False,active=False,error=str(exc),status='입력 오류로 정지')
        self.publish()

    def release(self):
        self.timer.stop()
        try:
            if self.engine: self.engine.stop(disable=True)
        finally:
            try:
                if self.keyboards: self.keyboards.close()
            finally:
                if self.output: self.output.close()
                if self.focus: self.focus.close()
                if self.lock: self.lock.close()
                self.engine = self.keyboards = self.output = self.focus = self.lock = None

    def configure(self, settings):
        settings = validated_settings(settings)
        self.set_enabled(False)
        temp = self.settings_path.with_suffix('.tmp')
        temp.write_text(json.dumps(settings),encoding='utf-8'); temp.replace(self.settings_path)
        self.settings = settings

    def request_permissions(self):
        self.publish()

    def shutdown(self):
        self.release(); self.closing = True
        self.state.update(enabled=False,active=False,status='연타 꺼짐'); self.publish()
