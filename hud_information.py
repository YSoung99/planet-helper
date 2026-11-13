"""Live recognition information without presenting old observations as current."""
import time
from dataclasses import dataclass

UI_NAMES = {'classic': '클래식 UI', 'new': '새로운 UI'}


@dataclass
class HudInformation:
    ui_kind: str = 'unknown'
    generation: int = -1
    size: tuple = ()
    verified_at: float = 0
    observed_at: float = 0
    searching: bool = False

    def observe(self, details, generation, value, error, now):
        if error or value is None:
            self.searching = True
            return
        if (details.get('auto_confirmed') and details.get('auto_detected')
                and details.get('ui_kind') in UI_NAMES):
            self.ui_kind = details['ui_kind']
            self.generation = generation
            self.size = tuple(details.get('game_image_size') or ())
            self.verified_at = details.get('ui_verified_at') or now
            self.observed_at = now
            self.searching = False
        elif details.get('auto_detected'):
            self.searching = True

    def text(self, *, running, automatic, generation, now=None):
        now = time.monotonic() if now is None else now
        name = UI_NAMES.get(self.ui_kind)
        if not automatic:
            return '인식 UI: 수동 영역 · UI 미확인'
        if not running:
            return f'마지막 인식 UI: {name} · 측정 정지' if name else '인식 UI: 확인 전'
        current = (name and self.generation == generation and not self.searching
                   and now-self.observed_at <= 8 and now-self.verified_at <= 15)
        if current:
            return f'인식 UI: {name}'
        return f'인식 UI: 재확인 중 · 마지막 {name}' if name else '인식 UI: 확인 중'


def build_dialog(controller):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
    import tracker
    dialog = QDialog(controller.window)
    dialog.setWindowTitle('플래닛 도우미 정보')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setStyleSheet('QDialog {background:#151b24;} QLabel {color:#d5dee8;}')
    layout = QVBoxLayout(dialog)
    layout.setSpacing(12)
    version = QLabel('자동 인식 시험용 · '+tracker.APP_VERSION)
    ui = QLabel(); ui.setWordWrap(True); ui.setStyleSheet('color:#b4e9c6;font-size:15px;font-weight:600;')
    mode = QLabel(); size = QLabel(); freshness = QLabel()
    for widget in (version, ui, mode, size, freshness):
        layout.addWidget(widget)
    close = QPushButton('닫기'); close.clicked.connect(dialog.close); layout.addWidget(close)
    def refresh():
        state = controller.hud_information
        now = time.monotonic()
        ui.setText(state.text(running=controller.running, automatic=controller.auto_hud,
                              generation=controller.generation, now=now))
        mode.setText('인식 방식: 자동 탐색' if controller.auto_hud else '인식 방식: 수동 영역')
        size.setText(f'마지막 확인 화면: {state.size[0]} × {state.size[1]}' if len(state.size) == 2
                     else '게임 화면 크기: 확인 전')
        freshness.setText(f'UI 확인: {max(0, int(now-state.verified_at))}초 전' if state.verified_at
                          else '측정 시작 후 UI를 확인해요.')
    timer = QTimer(dialog); timer.timeout.connect(refresh); timer.start(500)
    dialog.finished.connect(timer.stop)
    refresh(); dialog.setMinimumWidth(365); dialog.show()
    return dialog
