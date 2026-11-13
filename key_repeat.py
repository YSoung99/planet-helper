"""Nonblocking bridge to the native input engine bundled in this application."""
import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

DEFAULT_SETTINGS = dict(firstDelayMs=230, minIntervalMs=85, maxIntervalMs=120,
                        attackTriggerKeyCode=0)


def validated_settings(value):
    """Accept only the native engine's bounded settings; never restore enabled."""
    result = dict(DEFAULT_SETTINGS)
    if not isinstance(value, dict):
        raise ValueError('연타 설정을 읽을 수 없어요')
    for key in result:
        if key in value:
            if isinstance(value[key], bool) or not isinstance(value[key], int):
                raise ValueError('연타 간격은 정수로 입력해 주세요')
            result[key] = value[key]
    if not 100 <= result['firstDelayMs'] <= 2000:
        raise ValueError('첫 대기는 100~2000ms로 입력해 주세요')
    if not 50 <= result['minIntervalMs'] <= result['maxIntervalMs'] <= 1000:
        raise ValueError('반복 간격은 50~1000ms로 입력하고 최솟값을 먼저 맞춰 주세요')
    if result['attackTriggerKeyCode'] not in (0, 6):
        raise ValueError('공격 키 설정을 확인해 주세요')
    return result


class KeyRepeatBridge(QObject):
    changed = Signal(dict)

    def __init__(self, data_path, helper_path=None, parent=None):
        super().__init__(parent)
        self.settings_path = Path(data_path)/'key-repeat.json'
        self.settings = dict(DEFAULT_SETTINGS)
        self.saved_settings = False
        self.settings_error = ''
        try:
            if self.settings_path.exists():
                self.settings = validated_settings(json.loads(self.settings_path.read_text(encoding='utf-8')))
                self.saved_settings = True
        except (OSError, ValueError, TypeError) as exc:
            self.settings_error = f'연타 설정 읽기 실패 · 기본값 사용: {exc}'
        resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        self.helper_path = Path(helper_path) if helper_path else resources/'native'/'KeyRepeatHelper'
        self.state = dict(enabled=False, active=False, accessibility=False, inputMonitoring=False,
                          legacyRunning=False, status='연타 준비 중', error='')
        self.buffer = bytearray()
        self.closing = False
        self.process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert('PLANET_EXP_DATA_DIR', str(Path(data_path).resolve()))
        self.process.setProcessEnvironment(environment)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read)
        # Drain diagnostics without exposing private paths in the main panel.
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.started.connect(self._started)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(lambda: self.send('status'))

    def start(self):
        if self.closing or self.process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.helper_path.is_file():
            self._publish_error('연타 모듈을 찾을 수 없어요 · 앱을 다시 설치해 주세요')
            return
        self.process.start(str(self.helper_path), [])

    def _started(self):
        # The helper defaults to disabled, and a persisted preference cannot arm it.
        if self.saved_settings:
            self.send('configure', enabled=False, **self.settings)
        self.send('status')
        self.poll_timer.start()

    def send(self, command, **values):
        if self.process.state() != QProcess.ProcessState.Running:
            return False
        payload = json.dumps(dict(command=command, **values), separators=(',', ':'))+'\n'
        return self.process.write(payload.encode('utf-8')) >= 0

    def _read(self):
        if self.closing:
            self.process.readAllStandardOutput()
            return
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer) > 262144:
            self.buffer.clear()
            self._protocol_error()
            return
        while b'\n' in self.buffer:
            line, _, remaining = self.buffer.partition(b'\n')
            self.buffer = bytearray(remaining)
            try:
                state = json.loads(line)
                if not isinstance(state, dict) or state.get('type') != 'status':
                    raise ValueError('invalid status')
                for key in ('enabled', 'active', 'accessibility', 'inputMonitoring', 'legacyRunning'):
                    if not isinstance(state.get(key), bool):
                        raise ValueError('invalid status')
                if 'settings' in state:
                    self.settings = validated_settings(state['settings'])
                self.state = state
                self.changed.emit(dict(state))
            except (ValueError, TypeError, UnicodeError):
                self._protocol_error()
                return

    def _protocol_error(self):
        self.shutdown()
        self._publish_error('연타 상태 응답을 읽을 수 없어 연타를 종료했어요 · 앱을 다시 실행해 주세요')

    def _publish_error(self, message):
        self.state.update(enabled=False, active=False, status=message, error=message)
        self.changed.emit(dict(self.state))

    def _finished(self, *_):
        self.poll_timer.stop()
        self.buffer.clear()
        if self.closing:
            self.state.update(enabled=False, active=False)
            if not self.state.get('error'):
                self.state.update(status='연타 꺼짐', error='')
            self.changed.emit(dict(self.state))
        else:
            self._publish_error('연타 모듈이 종료됐어요 · 앱을 다시 실행해 주세요')

    def _error(self, error):
        if not self.closing:
            self.shutdown()
            self._publish_error('연타 모듈을 실행할 수 없어요 · 앱을 다시 실행해 주세요')

    def set_enabled(self, enabled):
        if self.closing:
            return False
        if enabled and self.state.get('legacyRunning'):
            self._publish_error('기존 키 반복 앱을 종료한 뒤 연타를 켜 주세요')
            return False
        if not self.send('configure', enabled=bool(enabled), **self.settings):
            self._publish_error('연타 모듈이 준비되지 않았어요 · 앱을 다시 실행해 주세요')
            return False
        return True

    def configure(self, settings):
        selected = validated_settings(settings)
        if not self.send('configure', enabled=False, **selected):
            raise RuntimeError('연타 모듈이 준비되지 않았어요')
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(selected, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.settings_path)
        self.settings = selected
        self.saved_settings = True

    def request_permissions(self):
        if not self.send('request_permissions'):
            self._publish_error('연타 모듈이 준비되지 않았어요 · 앱을 다시 실행해 주세요')

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.poll_timer.stop()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.send('configure', enabled=False, **self.settings)
            self.send('shutdown')
            self.process.closeWriteChannel()
            if not self.process.waitForFinished(1000):
                self.process.terminate()
                if not self.process.waitForFinished(500):
                    self.process.kill()
                    self.process.waitForFinished(500)
        self.state.update(enabled=False, active=False)
