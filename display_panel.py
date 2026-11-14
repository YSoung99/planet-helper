"""Compact and detailed views share one controller, session and input engine."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QSpinBox, QVBoxLayout,
                              QHBoxLayout, QGridLayout, QCheckBox)
from key_repeat import KeyRepeatBridge


def build(c, config, application, level_value, repeat_bridge=KeyRepeatBridge):
    c.compact = config.get('compact', True)
    c.always_on_top = config.get('always_on_top', True)
    c.window_opacity = config.get('window_opacity', .90)
    c.rate_display_key = None
    c.rate_inputs_key = None
    c.window.setWindowTitle('플래닛 도우미 자동인식 시험용')
    c.window.setObjectName('panel')
    c.window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    c.window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, c.always_on_top)
    c.window.setStyleSheet("""
        QWidget { color:#e4ecf4; font-size:12px; }
        QWidget#panel, QWidget#body { background:transparent; border:none; }
        QWidget#metricsCard, QWidget#recognitionCard {
            background:#172434; border:1px solid #26374a; border-radius:12px;
        }
        QLabel { background:transparent; border:none; }
        QLabel#muted { color:#a5b7ca; }
        QLabel#metric { color:#e4ecf4; font-size:19px; font-weight:600; }
        QLabel#total { color:#e1fcef; font-size:40px; font-weight:700; }
        QPushButton {
            background:#25364a; color:#e0eaf5; border:none;
            border-radius:6px; padding:5px 9px;
        }
        QPushButton:hover { background:#334c65; }
        QPushButton:pressed { background:#3e5871; }
        QPushButton:checked { background:#244c46; color:#b9f4dc; }
        QPushButton:disabled { color:#8497ab; }
        QPushButton#toggle { background:#83e5bb; color:#112a22; font-weight:700; }
        QPushButton#toggle:hover { background:#a8f2d2; }
        QPushButton#toggle[running="true"] { background:#f6cc92; color:#3d281a; }
        QSpinBox { background:#24364a; color:#d3ecff; border:none; padding:3px; }
        QMenu { background:#1b2939; color:#e4ecf4; }
        QMenu::item:selected { background:#334c65; }
    """)
    layout = QVBoxLayout(c.window)
    layout.setContentsMargins(12,10,12,10); layout.setSpacing(6)
    header = QHBoxLayout(); header.setSpacing(5)
    title = c.label('플래닛 도우미');c.title_label=title;title.setStyleSheet('font-size:16px;font-weight:700;color:#f0f4f8;')
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
    c.pin_button=QPushButton();c.pin_button.setCheckable(True)
    c.pin_button.setChecked(c.always_on_top);c.pin_button.setAccessibleName('항상 맨 위 고정')
    c.pin_button.toggled.connect(c.toggle_pin)
    c.history_button = QPushButton('기록')
    c.history_button.setAccessibleName('사냥 기록 비교')
    c.history_button.clicked.connect(c.show_history)
    header.addWidget(c.history_button)
    c.options_button = QPushButton('설정')
    c.options_button.clicked.connect(
        lambda: c.context_menu(c.options_button.geometry().bottomLeft()))
    header.addWidget(c.options_button)
    close=QPushButton('×');c.close_button=close;close.setFixedWidth(22);close.setAccessibleName('종료')
    close.clicked.connect(c.close);header.addWidget(close);layout.addLayout(header)

    c.compact_body=QWidget();c.compact_body.setObjectName('metricsCard')
    grid=QGridLayout(c.compact_body);grid.setContentsMargins(11,6,11,6);grid.setHorizontalSpacing(18)
    c.compact_total=c.label('0','metric');c.compact_total.setStyleSheet('color:#f4f8fc;font-size:26px;font-weight:600;')
    c.compact_average=c.label('—','metric');c.compact_six_average=c.label('—','metric')
    c.compact_headings=[]
    for col,(label,value) in enumerate((('총 획득 경험치',c.compact_total),('누적 분당 평균',c.compact_average),('최근 3분 · 분당',c.compact_six_average))):
        heading=c.label(label);c.compact_headings.append(heading);grid.addWidget(heading,0,col);grid.addWidget(value,1,col);grid.setColumnStretch(col,1)
    c.compact_level_eta = c.label('레벨업 예상 —')
    c.compact_level_eta.setAccessibleName('레벨업 예상 시간')
    c.compact_level_eta.setStyleSheet('color:#e8d9a8;font-size:10px;')
    grid.addWidget(c.compact_level_eta, 2, 2)
    layout.addWidget(c.compact_body)
    c.full_body=QWidget();c.full_body.setObjectName('body')
    # Detailed view: measurements on the left, recognition status on the right.
    columns=QHBoxLayout(c.full_body);columns.setContentsMargins(0,0,0,0);columns.setSpacing(16)
    metrics_card = QWidget()
    metrics_card.setObjectName('metricsCard')
    body = QVBoxLayout(metrics_card)
    body.setContentsMargins(14,12,14,12)
    body.setSpacing(7)
    columns.addWidget(metrics_card, 6)
    recognition_card = QWidget()
    recognition_card.setObjectName('recognitionCard')
    side = QVBoxLayout(recognition_card)
    side.setContentsMargins(13,12,13,12)
    side.setSpacing(6)
    columns.addWidget(recognition_card, 5)
    body.addWidget(c.label('총 획득 경험치'))
    c.total_label=c.label('0','total');body.addWidget(c.total_label);body.addWidget(c.line())
    grid=QGridLayout();grid.setHorizontalSpacing(15);grid.setVerticalSpacing(7)
    grid.addWidget(c.label('누적 분당 평균'),0,0);grid.addWidget(c.label('최근 3분 · 분당'),0,1)
    c.minute_label=c.label('—','metric');c.six_label=c.label('—','metric')
    grid.addWidget(c.minute_label,1,0);grid.addWidget(c.six_label,1,1)
    grid.addWidget(c.label('직전 1분 획득 ≈'),2,0);grid.addWidget(c.label('최근 6분 획득 ≈'),2,1)
    c.recent_minute=c.label('집계 대기','metric');c.recent_six=c.label('집계 대기','metric')
    grid.addWidget(c.recent_minute,3,0);grid.addWidget(c.recent_six,3,1);body.addLayout(grid)
    c.minute_info=c.label('첫 증가 후 1분마다 집계');body.addWidget(c.minute_info)
    c.rate_info=c.label('평균은 60초 관측 후 표시 · 10초마다 갱신');c.rate_info.setWordWrap(True);body.addWidget(c.rate_info)
    body.addStretch()
    recognition_title = c.label('게임 화면 인식', 'metric')
    recognition_title.setStyleSheet('font-size:14px;')
    side.addWidget(recognition_title)
    c.source_label=c.label('게임 창 확인 대기')
    c.source_label.setWordWrap(True)
    side.addWidget(c.source_label)
    c.current_label=c.label(c.value.get());c.current_label.setStyleSheet('color:#b4e9c6;')
    c.baseline_label=c.label('시작 기준 —');c.table_label=c.label('')
    c.detail_label=c.label(c.status.get());c.detail_label.setWordWrap(True);c.detail_label.setMinimumHeight(32)
    for widget in (c.current_label,c.baseline_label,c.table_label,c.detail_label):side.addWidget(widget)
    c.auto_checkbox=QCheckBox('레벨·경험치 자동 인식')
    c.auto_checkbox.setChecked(c.auto_hud)
    c.auto_checkbox.toggled.connect(c.toggle_auto_hud)
    side.addWidget(c.auto_checkbox)
    character_controls = QHBoxLayout()
    character_controls.addWidget(c.level_prefix)
    character_controls.addWidget(c.level_picker)
    character_controls.addStretch()
    character_controls.addWidget(c.pin_button)
    character_controls.addWidget(c.jev_button)
    side.addLayout(character_controls)
    info_row=QHBoxLayout()
    c.ui_label=c.label('인식 UI: 확인 전');c.ui_label.setWordWrap(True)
    c.ui_label.setAccessibleName('현재 인식 UI')
    info_row.addWidget(c.ui_label,1)
    c.info_button=QPushButton('정보');c.info_button.clicked.connect(c.show_information)
    info_row.addWidget(c.info_button);side.addLayout(info_row)
    c.preview=QLabel('측정 시작 후 표시');c.preview.setFixedHeight(35)
    c.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
    c.preview.setStyleSheet('background:#101721;border:1px solid #2b3542;color:#84919f;')
    side.addWidget(c.preview);side.addStretch();layout.addWidget(c.full_body)
    c.level_eta=c.label('레벨업 예상 · 평균 집계 후 표시');c.level_eta.setWordWrap(True)
    c.level_eta.setStyleSheet('color:#e8d9a8;');c.level_eta.setAccessibleName('시간당 경험치와 레벨업 예상 시간')
    layout.addWidget(c.level_eta)
    c.activity_label = c.label('사냥 신호 · 측정 대기')
    body.addWidget(c.activity_label)
    c.jev_status = c.label('')
    c.jev_status.setWordWrap(True)
    c.jev_status.hide()
    layout.addWidget(c.jev_status)
    c.change_prompt=QPushButton();c.change_prompt.setVisible(False)
    c.change_prompt.setStyleSheet('QPushButton{background:#3a3322;color:#f0d98c;text-align:left;padding:4px 6px;}')
    c.change_prompt.setAccessibleName('효율 변화 태그 달기')
    c.change_prompt.clicked.connect(c.answer_change_prompt);layout.addWidget(c.change_prompt)
    status=QHBoxLayout()
    c.timer_label=c.label('사냥 00:00:00')
    c.timer_label.setAccessibleName('순수 사냥 시간')
    status.addWidget(c.timer_label)
    status.addStretch();c.footer=c.label('● 측정 대기');status.addWidget(c.footer);layout.addLayout(status)
    buttons=QHBoxLayout();c.measurement_controls=buttons;buttons.setSpacing(5)
    c.region_button=QPushButton('영역 지정');c.region_button.clicked.connect(c.select)
    c.reset_button=QPushButton('새 측정');c.reset_button.clicked.connect(c.reset)
    c.export_button=QPushButton('CSV 저장');c.export_button.clicked.connect(c.export)
    c.toggle_button=QPushButton('측정 시작');c.toggle_button.setObjectName('toggle');c.toggle_button.clicked.connect(c.toggle)
    c.recent_button = QPushButton('최근 5분 포함')
    c.recent_button.clicked.connect(c.start_with_recent)
    c.recent_button.setEnabled(False)
    c.recent_button.setVisible(c.auto_hud and c.recent_recording)
    for button in (c.region_button,c.reset_button,c.export_button,c.toggle_button,c.recent_button):buttons.addWidget(button)
    controls=QHBoxLayout();controls.setSpacing(14);controls.addLayout(buttons);layout.addLayout(controls)
    c.key_repeat=repeat_bridge(__import__('tracker').BASE,parent=c.window)
    repeat=QHBoxLayout();c.repeat_controls=repeat;c.repeat_title=c.label('A·Enter 연타');repeat.addWidget(c.repeat_title)
    c.repeat_status=c.label('연타 꺼짐 · 준비 중');c.repeat_status.setAccessibleName('연타 상태')
    repeat.addStretch()
    c.repeat_button=QPushButton('연타 켜기');c.repeat_button.setEnabled(False);c.repeat_button.clicked.connect(c.toggle_repeat)
    repeat.addWidget(c.repeat_button)
    c.repeat_settings_button=QPushButton('연타 설정');c.repeat_settings_button.setAccessibleName('연타 설정과 권한')
    c.repeat_settings_button.clicked.connect(c.repeat_settings);repeat.addWidget(c.repeat_settings_button);controls.addLayout(repeat,1)
    side.addWidget(c.repeat_status)
    c.repeat_status.setMinimumWidth(0)
    c.key_repeat.changed.connect(c.update_repeat);application.aboutToQuit.connect(c.key_repeat.shutdown)
    c.window.move(*config.get('position',[application.primaryScreen().availableGeometry().right()-640,90]))
    resize(c)


def resize(c):
    c.title_label.setText('플래닛 도우미')
    c.mode_button.setText('상세' if c.compact else '접기')
    c.pin_button.setText('맨 위 켬' if c.always_on_top else '맨 위 끔')
    c.version_label.setVisible(not c.compact)
    c.level_prefix.setVisible(not c.compact);c.level_picker.setVisible(not c.compact)
    c.rate_info.setVisible(not c.compact);c.repeat_status.setVisible(not c.compact)
    c.repeat_title.setVisible(not c.compact)
    for button in (c.region_button,c.reset_button,c.export_button):button.setVisible(not c.compact)
    c.compact_headings[2].setVisible(True);c.compact_six_average.setVisible(True)
    c.compact_headings[2].setText('6분 환산')
    c.compact_headings[0].setText('총 획득 경험치');c.compact_headings[1].setText('누적 분당 평균')
    c.compact_body.layout().setColumnStretch(2,1)
    c.compact_body.layout().setHorizontalSpacing(10)
    c.compact_total.setStyleSheet('color:#b9f7d9;font-size:22px;font-weight:700;')
    c.compact_average.setStyleSheet('color:#b4e9c6;font-size:16px;font-weight:600;')
    c.compact_six_average.setStyleSheet('color:#b4e9c6;font-size:16px;font-weight:600;')
    c.compact_six_average.setToolTip('누적 분당 평균 × 6 · 같은 관측 기준의 6분 환산값')
    c.window.layout().setContentsMargins(8 if c.compact else 12,8,8 if c.compact else 12,8)
    c.window.layout().setSpacing(5 if c.compact else 8)
    for button in (c.jev_button,c.mode_button,c.pin_button,c.close_button,c.toggle_button,c.recent_button,c.repeat_button,c.repeat_settings_button,c.history_button,c.options_button):
        button.setStyleSheet('padding:3px 5px;font-size:10px;' if c.compact else '')
    c.toggle_button.setStyleSheet('padding:4px 11px;' if c.compact else 'padding:6px 14px;')
    if c.compact:
        c.repeat_controls.insertWidget(0,c.toggle_button)
        c.repeat_controls.insertWidget(1,c.recent_button)
    else:
        c.measurement_controls.addWidget(c.toggle_button)
        c.measurement_controls.addWidget(c.recent_button)

    c.compact_body.setVisible(c.compact);c.full_body.setVisible(not c.compact)
    c.level_eta.setVisible(not c.compact)
    c.timer_label.setStyleSheet('color:#c7e9de;font-size:12px;font-weight:600;' if c.compact else '')
    c.activity_label.setVisible(not c.compact)
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
