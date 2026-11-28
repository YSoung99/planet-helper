"""Compact and detailed views share one controller, session and input engine."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QSpinBox, QVBoxLayout,
                              QHBoxLayout, QGridLayout, QCheckBox)
from key_repeat import KeyRepeatBridge


def add_stat(c, grid, row, column, title, value):
    cell = QVBoxLayout()
    cell.setSpacing(1)
    heading = c.label(title, 'statCaption')
    value.setAccessibleName(title)
    font = value.font()
    font.setFeature(QFont.Tag('tnum'), 1)
    value.setFont(font)
    cell.addWidget(heading)
    cell.addWidget(value)
    grid.addLayout(cell, row, column)
    return heading


def build(c, config, application, level_value, repeat_bridge=KeyRepeatBridge):
    c.compact = config.get('compact', True)
    c.always_on_top = config.get('always_on_top', True)
    c.window_opacity = config.get('window_opacity', .90)
    c.rate_display_key = None
    c.window_display_key = None
    c.rate_inputs_key = None
    c.window.setWindowTitle(c.APP_TITLE)
    c.window.setObjectName('panel')
    c.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    c.window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, c.always_on_top)
    c.window.setStyleSheet("""
        QWidget { color:#eef2f7; font-size:12px; }
        QWidget#panel, QWidget#body { background:transparent; border:none; }
        QWidget#summaryCard {
            background:rgba(110,231,183,22); border:1px solid rgba(110,231,183,52); border-radius:14px;
        }
        QWidget#metricsCard, QWidget#recognitionCard {
            background:rgba(255,255,255,16); border:1px solid rgba(255,255,255,26); border-radius:14px;
        }
        QLabel#sectionTitle { color:#f5f7fa; font-size:13px; font-weight:700; }
        QLabel { background:transparent; border:none; }
        QLabel#muted { color:rgba(235,241,247,165); }
        QLabel#statCaption { color:rgba(235,241,247,150); font-size:11px; font-weight:600; }
        QLabel#metric { color:#f2f5f9; font-size:19px; font-weight:600; }
        QLabel#primaryStat { color:#a7f3d0; font-size:24px; font-weight:700; }
        QLabel#statValue { color:#f8fafc; font-size:20px; font-weight:650; }
        QPushButton {
            background:rgba(255,255,255,22); color:#eef2f7; border:1px solid rgba(255,255,255,18);
            border-radius:9px; padding:5px 11px;
        }
        QPushButton:hover { background:rgba(255,255,255,36); }
        QPushButton:pressed { background:rgba(255,255,255,50); }
        QPushButton:checked { background:rgba(110,231,183,46); color:#c7f9e3; border-color:rgba(110,231,183,70); }
        QPushButton:disabled { color:rgba(235,241,247,95); background:rgba(255,255,255,10); }
        QPushButton#toggle { background:#6ee7b7; color:#052e21; font-weight:700; border:none; }
        QPushButton#toggle:hover { background:#95f0cc; }
        QPushButton#toggle[running="true"] { background:#fbbf77; color:#3b2408; }
        QPushButton#close { background:transparent; border:none; color:rgba(235,241,247,150);
                            border-radius:11px; padding:0; font-size:14px; }
        QPushButton#close:hover { background:#ff5f57; color:#ffffff; }
        QSpinBox { background:rgba(255,255,255,22); color:#eef2f7; border:none; border-radius:6px; padding:3px; }
        QCheckBox { spacing:6px; }
        QMenu {
            background:#20252e; color:#eef2f7; border:1px solid rgba(255,255,255,30);
            padding:5px;
        }
        QMenu::item { padding:6px 26px 6px 22px; border-radius:6px; }
        QMenu::item:selected { background:rgba(110,231,183,60); }
        QMenu::item:disabled { color:rgba(235,241,247,110); }
        QMenu::separator { height:1px; background:rgba(255,255,255,26); margin:4px 8px; }
    """)
    layout = QVBoxLayout(c.window)
    layout.setContentsMargins(14,12,14,12); layout.setSpacing(6)
    header = QHBoxLayout(); header.setSpacing(5); c.header_layout = header
    title = c.label('플래닛 도우미');c.title_label=title;title.setStyleSheet('font-size:15px;font-weight:700;color:#f8fafc;')
    title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    header.addWidget(title)
    c.version_label = c.label(__import__('tracker').APP_VERSION);header.addWidget(c.version_label)
    header.addStretch();c.level_prefix=c.label('Lv.')
    c.level_picker = QSpinBox();c.level_picker.setRange(1,219)
    c.level_picker.setEnabled(not c.auto_hud)
    c.level_picker.setValue(c.session.level);c.level_picker.setFixedWidth(52)
    c.level_picker.setAccessibleName('캐릭터 레벨');c.level_var=level_value(c.level_picker)
    c.level_picker.valueChanged.connect(c.change_level)
    c.jev_button = QPushButton()
    c.jev_button.setCheckable(True)
    c.jev_button.setAccessibleName('JEV 사냥터 이름 확인')
    c.update_jev_button()
    c.jev_button.toggled.connect(c.toggle_jev)

    c.mode_button=QPushButton();c.mode_button.setAccessibleName('간단 화면 전환')
    c.mode_button.clicked.connect(c.toggle_display);header.addWidget(c.mode_button)
    c.history_button = QPushButton('기록')
    c.history_button.setAccessibleName('사냥 기록 비교')
    c.history_button.clicked.connect(c.show_history)
    header.addWidget(c.history_button)
    c.options_button = QPushButton('설정')
    c.options_button.clicked.connect(
        lambda: c.context_menu(c.options_button.geometry().bottomLeft()))
    header.addWidget(c.options_button)
    close=QPushButton('✕');c.close_button=close;close.setObjectName('close');close.setFixedSize(22,22);close.setAccessibleName('종료')
    close.clicked.connect(c.close);header.addWidget(close);layout.addLayout(header)

    c.compact_body=QWidget();c.compact_body.setObjectName('metricsCard')
    grid=QGridLayout(c.compact_body);grid.setContentsMargins(10,5,10,5)
    grid.setHorizontalSpacing(12);grid.setVerticalSpacing(2)
    grid.setColumnStretch(0,1);grid.setColumnStretch(1,1)
    c.compact_time=c.label('00:00:00','primaryStat')
    c.compact_recent_six=c.label('—','primaryStat')
    c.compact_average=c.label('—','statValue');c.compact_recent_minute=c.label('—','statValue')
    add_stat(c,grid,0,0,'순수 사냥 시간',c.compact_time)
    c.compact_six_heading=add_stat(c,grid,0,1,'최근 6분 획득',c.compact_recent_six)
    add_stat(c,grid,1,0,'누적 분당 평균',c.compact_average)
    add_stat(c,grid,1,1,'직전 1분 획득',c.compact_recent_minute)
    c.compact_meso = c.label('메소 대기 · 인벤토리 열면 측정')
    c.compact_meso.setAccessibleName('간단 화면 메소 지표')
    c.compact_meso.setStyleSheet('color:#fde68a;font-size:10px;font-weight:600;')
    compact_meso_font = c.compact_meso.font()
    compact_meso_font.setFeature(QFont.Tag('tnum'), 1)
    c.compact_meso.setFont(compact_meso_font)
    grid.addWidget(c.compact_meso, 2, 0, 1, 2)
    c.compact_level_eta = c.label('레벨업 예상 —')
    c.compact_level_eta.setAccessibleName('레벨업 예상 시간')
    c.compact_level_eta.setStyleSheet('color:#fcd99a;font-size:10px;')
    layout.addWidget(c.compact_body)
    c.full_body = QWidget()
    c.full_body.setObjectName('body')
    detail = QVBoxLayout(c.full_body)
    detail.setContentsMargins(0, 0, 0, 0)
    detail.setSpacing(9)

    summary = QWidget()
    summary.setObjectName('summaryCard')
    summary_grid = QGridLayout(summary)
    summary_grid.setContentsMargins(14, 11, 14, 10)
    summary_grid.setHorizontalSpacing(15)
    summary_grid.setVerticalSpacing(8)
    for column in range(3):
        summary_grid.setColumnStretch(column, 1)
    c.timer_label = c.label('00:00:00', 'primaryStat')
    c.total_label = c.label('0', 'primaryStat')
    c.meso_delta_label = c.label('—', 'primaryStat')
    add_stat(c, summary_grid, 0, 0, '순수 사냥 시간', c.timer_label)
    add_stat(c, summary_grid, 0, 1, '총 획득 EXP', c.total_label)
    add_stat(c, summary_grid, 0, 2, '메소 순증감', c.meso_delta_label)
    c.meso_delta_label.setToolTip('시작 잔액과 마지막 확인 잔액의 차이에서 분류한 거래·구매를 제외한 값입니다.')
    c.meso_label = c.label('메소 · 측정 중 인벤토리를 열면 기록')
    c.meso_label.setAccessibleName('인벤토리 메소 잔액')
    c.meso_label.setStyleSheet('font-size:10px;')
    c.meso_label.setToolTip('대기 중이나 측정 중 인벤토리를 열면 잔액을 읽고 사냥 시간 기준으로 시급을 계산합니다.')
    c.meso_status_label = c.label('시작 잔액 대기 · 종료 잔액 대기')
    c.meso_status_label.setAccessibleName('시작과 종료 메소 잔액 확인 상태')
    c.meso_status_label.setStyleSheet('font-size:10px;')
    c.meso_status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    meso_row = QHBoxLayout()
    meso_row.addWidget(c.meso_label, 1)
    meso_row.addWidget(c.meso_status_label)
    summary_grid.addLayout(meso_row, 1, 0, 1, 3)
    c.meso_rate_label = c.label('시간당 메소 · 측정 중 인벤토리를 열면 집계')
    c.meso_rate_label.setAccessibleName('실시간 메소 효율과 구간 흐름')
    c.meso_rate_label.setStyleSheet('color:#fde68a;font-size:10px;font-weight:600;')
    meso_rate_font = c.meso_rate_label.font()
    meso_rate_font.setFeature(QFont.Tag('tnum'), 1)
    c.meso_rate_label.setFont(meso_rate_font)
    summary_grid.addWidget(c.meso_rate_label, 2, 0, 1, 3)
    c.potion_label = c.label('')
    c.potion_label.setAccessibleName('단축키 포션 소모량과 물약비')
    c.potion_label.setStyleSheet('color:#93c5fd;font-size:10px;font-weight:600;')
    potion_font = c.potion_label.font()
    potion_font.setFeature(QFont.Tag('tnum'), 1)
    c.potion_label.setFont(potion_font)
    c.potion_label.setVisible(bool(getattr(c, 'potion_slots', ())))
    summary_grid.addWidget(c.potion_label, 3, 0, 1, 3)
    detail.addWidget(summary)

    columns = QHBoxLayout()
    columns.setSpacing(10)
    detail.addLayout(columns)
    metrics_card = QWidget()
    metrics_card.setObjectName('metricsCard')
    rates = QVBoxLayout(metrics_card)
    rates.setContentsMargins(13, 10, 13, 10)
    rates.setSpacing(8)
    rates.addWidget(c.label('경험치 흐름', 'sectionTitle'))
    columns.addWidget(metrics_card, 1)
    grid = QGridLayout()
    grid.setHorizontalSpacing(11)
    grid.setVerticalSpacing(8)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    c.minute_label = c.label('—', 'statValue')
    c.recent_six = c.label('—', 'statValue')
    c.recent_minute = c.label('—', 'metric')
    c.six_label = c.label('—', 'metric')
    add_stat(c, grid, 0, 0, '누적 분당 평균', c.minute_label)
    c.six_heading = add_stat(c, grid, 0, 1, '최근 6분 획득', c.recent_six)
    add_stat(c, grid, 1, 0, '직전 1분 획득', c.recent_minute)
    add_stat(c, grid, 1, 1, '최근 3분 분당 평균', c.six_label)
    rates.addLayout(grid)
    rates.addStretch()

    recognition_card = QWidget()
    recognition_card.setObjectName('recognitionCard')
    side = QVBoxLayout(recognition_card)
    side.setContentsMargins(13, 10, 13, 10)
    side.setSpacing(5)
    columns.addWidget(recognition_card, 1)
    recognition_header = QHBoxLayout()
    recognition_header.addWidget(c.label('게임 인식', 'sectionTitle'))
    recognition_header.addStretch()
    c.info_button = QPushButton('인식 정보')
    c.info_button.clicked.connect(c.show_information)
    recognition_header.addWidget(c.info_button)
    side.addLayout(recognition_header)
    c.source_label = c.label('게임 창 확인 대기')
    c.source_label.setWordWrap(True)
    side.addWidget(c.source_label)
    c.current_label = c.label(c.value.get())
    c.current_label.setStyleSheet('color:#a7f3d0;')
    side.addWidget(c.current_label)
    c.ui_label = c.label('인식 UI: 확인 전')
    c.ui_label.setAccessibleName('현재 인식 UI')
    c.detail_label = c.label(c.status.get())
    c.detail_label.setWordWrap(True)
    c.detail_label.setStyleSheet('font-size:10px;')
    side.addWidget(c.detail_label)
    # Lines that appear only while they have something to say.
    c.jev_status = c.label('')
    c.jev_status.setWordWrap(True)
    c.jev_status.setStyleSheet('color:#c4b5fd;font-size:10px;')
    c.jev_status.hide()
    c.stall_label = c.label('')
    c.stall_label.setWordWrap(True)
    c.stall_label.setStyleSheet('color:#fcd99a;font-size:10px;')
    c.stall_label.hide()
    c.buff_label = c.label('경험치 버프 · 아이콘 미등록')
    c.buff_label.setStyleSheet('font-size:10px;')
    c.buff_label.setVisible(c.buff_has_templates)
    for line in (c.jev_status, c.stall_label, c.buff_label):
        side.addWidget(line)
    c.auto_checkbox = QCheckBox('레벨·경험치 자동 인식')
    c.auto_checkbox.setChecked(c.auto_hud)
    c.auto_checkbox.toggled.connect(c.toggle_auto_hud)
    auto_controls = QHBoxLayout()
    auto_controls.addWidget(c.auto_checkbox)
    auto_controls.addStretch()
    auto_controls.addWidget(c.jev_button)
    side.addLayout(auto_controls)
    c.manual_controls = QWidget()
    manual_controls = QHBoxLayout(c.manual_controls)
    manual_controls.setContentsMargins(0, 0, 0, 0)
    manual_controls.addWidget(c.level_prefix)
    manual_controls.addWidget(c.level_picker)
    c.region_button = QPushButton('영역 지정')
    c.region_button.clicked.connect(c.select)
    manual_controls.addWidget(c.region_button)
    manual_controls.addStretch()
    side.addWidget(c.manual_controls)
    side.addStretch()

    c.level_eta = c.label('레벨업 예상 · 평균 집계 후 표시')
    c.level_eta.setWordWrap(True)
    c.level_eta.setStyleSheet('color:#fcd99a;')
    c.level_eta.setAccessibleName('시간당 경험치와 레벨업 예상 시간')
    detail.addWidget(c.level_eta)
    layout.addWidget(c.full_body)

    # These live values are shown together in the recognition dialog.
    diagnostics = QWidget(c.window)
    diagnostics.hide()
    c.minute_info = c.label('첫 증가 후 1분마다 집계')
    c.rate_info = c.label('평균은 60초 관측 후 표시 · 10초마다 갱신')
    c.baseline_label = c.label('시작 기준 —')
    c.table_label = c.label('')
    c.activity_label = c.label('사냥 신호 · 측정 대기')
    c.repeat_status = c.label('연타 꺼짐 · 준비 중')
    for widget in (c.minute_info, c.rate_info, c.baseline_label, c.table_label,
                   c.activity_label, c.repeat_status, c.ui_label):
        widget.setParent(diagnostics)
    c.preview = QLabel(diagnostics)
    c.preview.setFixedSize(290, 34)
    c.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    c.meso_preview = QLabel(diagnostics)
    c.meso_preview.setFixedSize(290, 34)
    c.meso_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)

    c.change_prompt = QPushButton()
    c.change_prompt.hide()
    c.change_prompt.setStyleSheet('QPushButton{background:rgba(251,191,119,38);color:#fde3b5;border:1px solid rgba(251,191,119,70);text-align:left;padding:5px 9px;}')
    c.change_prompt.setAccessibleName('효율 변화 태그 달기')
    c.change_prompt.clicked.connect(c.answer_change_prompt)
    layout.addWidget(c.change_prompt)
    c.meso_change_prompt = QPushButton()
    c.meso_change_prompt.hide()
    c.meso_change_prompt.setStyleSheet('QPushButton{background:rgba(253,230,138,32);color:#fef08a;border:1px solid rgba(253,230,138,70);text-align:left;padding:5px 9px;}')
    c.meso_change_prompt.setAccessibleName('큰 메소 변동 분류')
    c.meso_change_prompt.clicked.connect(c.classify_meso_change)
    layout.addWidget(c.meso_change_prompt)
    status=QHBoxLayout()
    status.addWidget(c.compact_level_eta)
    c.time_info=c.label('');status.addWidget(c.time_info)
    status.addStretch();c.footer=c.label('● 측정 대기');status.addWidget(c.footer);layout.addLayout(status)
    buttons=QHBoxLayout();c.measurement_controls=buttons;buttons.setSpacing(5)
    c.reset_button = QPushButton('새 측정')
    c.reset_button.clicked.connect(c.reset)
    c.toggle_button = QPushButton('측정 시작')
    c.toggle_button.setObjectName('toggle')
    c.toggle_button.clicked.connect(c.toggle)
    c.recent_button = QPushButton('최근 5분 포함')
    c.recent_button.clicked.connect(c.start_with_recent)
    c.recent_button.setEnabled(False)
    c.recent_button.setVisible(c.auto_hud and c.recent_recording)
    for button in (c.toggle_button, c.reset_button, c.recent_button):
        buttons.addWidget(button)
    controls = QHBoxLayout()
    controls.setSpacing(10)
    controls.addLayout(buttons)
    layout.addLayout(controls)
    c.key_repeat = repeat_bridge(__import__('tracker').BASE, parent=c.window)
    repeat = QHBoxLayout()
    c.repeat_controls = repeat
    repeat.addStretch()
    c.repeat_button = QPushButton('연타 켜기')
    c.repeat_button.setEnabled(False)
    c.repeat_button.clicked.connect(c.toggle_repeat)
    repeat.addWidget(c.repeat_button)
    c.repeat_settings_button = QPushButton('연타 설정')
    c.repeat_settings_button.setAccessibleName('연타 설정과 권한')
    c.repeat_settings_button.clicked.connect(c.repeat_settings)
    repeat.addWidget(c.repeat_settings_button)
    controls.addLayout(repeat, 1)
    c.key_repeat.changed.connect(c.update_repeat);application.aboutToQuit.connect(c.key_repeat.shutdown)
    c.window.move(*config.get('position',[application.primaryScreen().availableGeometry().right()-640,90]))
    resize(c)


def resize(c):
    c.title_label.setText('플래닛 도우미')
    c.mode_button.setText('상세' if c.compact else '접기')
    c.version_label.setVisible(not c.compact)
    c.manual_controls.setVisible(not c.auto_hud)
    c.window.layout().setContentsMargins(10 if c.compact else 14,8 if c.compact else 10,10 if c.compact else 14,8 if c.compact else 10)
    c.window.layout().setSpacing(4 if c.compact else 8)
    for button in (c.jev_button,c.mode_button,c.toggle_button,c.reset_button,c.recent_button,c.repeat_button,c.repeat_settings_button,c.history_button,c.options_button):
        button.setStyleSheet('padding:3px 7px;font-size:10px;border-radius:8px;' if c.compact else '')
    c.toggle_button.setStyleSheet('padding:4px 12px;border-radius:8px;' if c.compact else 'padding:6px 16px;border-radius:10px;')
    if c.compact:
        c.repeat_controls.insertWidget(0,c.toggle_button)
        c.repeat_controls.insertWidget(1,c.reset_button)
        c.repeat_controls.insertWidget(2,c.recent_button)
    else:
        for column, button in enumerate((c.toggle_button, c.reset_button, c.recent_button)):
            c.measurement_controls.insertWidget(column, button)

    c.compact_body.setVisible(c.compact);c.full_body.setVisible(not c.compact)
    c.level_eta.setVisible(not c.compact)
    c.compact_level_eta.setVisible(c.compact)
    c.time_info.setVisible(not c.compact)
    c.repeat_settings_button.setVisible(not c.compact)
    c.repeat_button.setToolTip('A·Enter 반복 입력. 자세한 설정은 상세 화면에서 열 수 있습니다.')
    c.window.setMinimumSize(0,0);c.window.setMaximumSize(16777215,16777215)
    c.window.setFixedWidth(360 if c.compact else 640)
    c.window.layout().activate();c.window.adjustSize()
    c.window.setFixedHeight(max(128,c.window.sizeHint().height()) if c.compact else c.window.sizeHint().height())
    screen=c.application.screenAt(c.window.pos()) or c.application.primaryScreen()
    rect=screen.availableGeometry()
    c.window.move(max(rect.left(),min(c.window.x(),rect.right()-c.window.width()+1)),
                  max(rect.top(),min(c.window.y(),rect.bottom()-c.window.height()+1)))
