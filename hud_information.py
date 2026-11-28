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
    from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton
    import tracker

    dialog = QDialog(controller.window)
    dialog.setWindowTitle('인식 정보')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setStyleSheet("""
        QDialog { background:#18222f; }
        QFrame#infoCard { background:#202e3f; border:1px solid #35465b; border-radius:8px; }
        QLabel { color:#d5e1ee; border:0; background:transparent; }
        QLabel#infoTitle { color:#eff5fa; font-size:13px; font-weight:700; }
        QLabel#infoMuted { color:#a8bacd; }
    """)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(14, 14, 14, 14)
    layout.setSpacing(9)

    def section(title):
        card = QFrame()
        card.setObjectName('infoCard')
        column = QVBoxLayout(card)
        column.setContentsMargins(12, 9, 12, 9)
        column.setSpacing(5)
        heading = QLabel(title)
        heading.setObjectName('infoTitle')
        column.addWidget(heading)
        layout.addWidget(card)
        return column

    def line(column):
        widget = QLabel()
        widget.setWordWrap(True)
        widget.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(widget)
        return widget

    recognized = section('게임 화면')
    ui = line(recognized)
    ui.setStyleSheet('color:#b4ead1; font-weight:600;')
    capture = line(recognized)
    live = line(recognized)
    preview = QLabel('판독 이미지 대기')
    preview.setAccessibleName('최근 경험치 판독 이미지')
    preview.setMinimumHeight(34)
    preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    recognized.addWidget(preview)

    meso_section = section('메소 인식')
    meso_info = line(meso_section)
    meso_preview = QLabel('메소 판독 이미지 대기')
    meso_preview.setAccessibleName('최근 메소 판독 이미지')
    meso_preview.setMinimumHeight(34)
    meso_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    meso_section.addWidget(meso_preview)

    measured = section('측정 상세')
    exp = line(measured)
    rates = line(measured)
    effects = section('보조 기능')
    effects_text = line(effects)

    footer = QHBoxLayout()
    version = QLabel(f'플래닛 도우미 {tracker.APP_VERSION}')
    version.setObjectName('infoMuted')
    footer.addWidget(version)
    footer.addStretch()
    close = QPushButton('닫기')
    close.clicked.connect(dialog.close)
    footer.addWidget(close)
    layout.addLayout(footer)
    close.setFocus()

    def update(widget, text):
        if widget.text() != text:
            widget.setText(text)

    preview_key = None
    meso_preview_key = None

    def refresh():
        nonlocal preview_key, meso_preview_key
        state = controller.hud_information
        now = time.monotonic()
        update(ui, state.text(running=controller.running, automatic=controller.auto_hud,
                              generation=controller.generation, now=now))
        mode = '자동 탐색' if controller.auto_hud else '수동 영역'
        size = f'{state.size[0]} × {state.size[1]}' if len(state.size) == 2 else '크기 확인 전'
        freshness = f'UI 확인 {max(0, int(now-state.verified_at))}초 전' if state.verified_at else 'UI 확인 대기'
        update(capture, f'{mode} · {size} · {freshness}')
        update(live, controller.detail_label.text())
        if hasattr(controller, 'meso_diagnostic_text'):
            update(meso_info, controller.meso_diagnostic_text(now))
        update(exp, '\n'.join(filter(None, (controller.baseline_label.text(), controller.table_label.text()))))
        update(rates, '\n'.join(filter(None, (controller.minute_info.text(), controller.rate_info.text(),
                                             controller.activity_label.text()))))
        engine = getattr(controller, 'engine', None)
        if engine is None:
            engine_line = 'OCR 엔진 · 준비 중'
        elif getattr(engine, 'read_exp', None) is not None:
            engine_line = 'OCR 엔진 · Rust 가속'
        elif getattr(engine, 'fast', None) is not None:
            engine_line = 'OCR 엔진 · Python 2단계 경량'
        else:
            engine_line = 'OCR 엔진 · Python 기본'
        arbiter = getattr(controller, 'jev_arbiter', None)
        jev_summary = arbiter.summary_text() if arbiter is not None and hasattr(arbiter, 'summary_text') else ''
        potion_line = ''
        potion_lbl = getattr(controller, 'potion_label', None)
        if potion_lbl is not None and potion_lbl.text():
            potion_line = potion_lbl.text()
        update(effects_text, '\n'.join(filter(None, (engine_line, controller.buff_label.text(),
                                                    potion_line, jev_summary, controller.jev_status.text(),
                                                    f'연타 · {controller.repeat_status.text()}'))))
        pixmap = controller.preview.pixmap()
        if pixmap is not None and not pixmap.isNull() and pixmap.cacheKey() != preview_key:
            preview.setPixmap(pixmap)
            preview_key = pixmap.cacheKey()
        meso_widget = getattr(controller, 'meso_preview', None)
        if meso_widget is not None:
            meso_pix = meso_widget.pixmap()
            if meso_pix is not None and not meso_pix.isNull() and meso_pix.cacheKey() != meso_preview_key:
                meso_preview.setPixmap(meso_pix)
                meso_preview_key = meso_pix.cacheKey()

    timer = QTimer(dialog)
    timer.timeout.connect(refresh)
    timer.start(500)
    dialog.finished.connect(timer.stop)
    refresh()
    dialog.setMinimumWidth(400)
    dialog.show()
    return dialog
