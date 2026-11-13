"""Past measurements with hunting-spot memos, for comparing efficiency."""
import json
from datetime import datetime
from pathlib import Path

import tracker

MEMO_FILE = 'memos.json'


def memo_path(base=None):
    return Path(base or tracker.BASE)/'records'/MEMO_FILE


def load_memos(base=None):
    try:
        data = json.loads(memo_path(base).read_text(encoding='utf-8'))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_memo(session_id, text, base=None):
    memos = load_memos(base)
    text = ' '.join(str(text).split())[:60]
    if text: memos[session_id] = text
    else: memos.pop(session_id, None)
    path = memo_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(memos, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return text


def summarize(csv_path):
    """One row per record; None when it has no usable hunting time."""
    csv_path = Path(csv_path)
    level = None
    try:
        level = json.loads(csv_path.with_suffix('.json').read_text(encoding='utf-8')).get('level')
    except (OSError, ValueError, AttributeError):
        pass
    try:
        session = tracker.load_saved_session(csv_path, level, 0.0)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
        return None
    if session is None or session.started is None:
        return None
    active, idle = session.active_elapsed(0.0), session.idle(0.0)
    gained = session.last[0]-session.first[0]
    if active < 60 or gained <= 0:
        return None
    try:
        started = datetime.strptime(csv_path.stem[:15], '%Y%m%d-%H%M%S')
    except ValueError:
        started = None
    return dict(session_id=csv_path.stem, started=started, level=level, active=active, idle=idle,
                gained=gained, per_minute=gained*60/active)


def collect(base=None):
    folder = Path(base or tracker.BASE)/'records'
    rows = []
    for path in sorted(folder.glob('[0-9]*.csv'), reverse=True):
        row = summarize(path)
        if row: rows.append(row)
    memos = load_memos(base)
    for row in rows:
        row['memo'] = memos.get(row['session_id'], '')
    return rows


def by_memo(rows):
    """Weighted per-minute average per hunting spot (total EXP / total hunting time)."""
    groups = {}
    for row in rows:
        if not row['memo']: continue
        gained, active, count = groups.get(row['memo'], (0, 0.0, 0))
        groups[row['memo']] = (gained+row['gained'], active+row['active'], count+1)
    return sorted(((memo, gained*60/active, active, count) for memo, (gained, active, count) in groups.items()),
                  key=lambda item: -item[1])


def clock(seconds):
    seconds = int(seconds)
    return f'{seconds//3600}:{seconds%3600//60:02d}' if seconds >= 3600 else f'{seconds//60}분'


def build_dialog(controller):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
                                   QHeaderView, QAbstractItemView)
    from qt_controller import short_exp
    dialog = QDialog(controller.window)
    dialog.setWindowTitle('사냥 기록 비교')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(720, 520)
    dialog.setStyleSheet('QDialog {background:#151b24;} QLabel {color:#d5dee8;}'
                         'QTableWidget {background:#1b2330;color:#d5dee8;gridline-color:#2b3542;}'
                         'QHeaderView::section {background:#25303d;color:#b8c4d0;border:none;padding:4px;}')
    layout = QVBoxLayout(dialog)
    hint = QLabel('사냥터 메모 칸을 두 번 눌러 입력하세요. 분당 평균은 휴식(경험치 1분 이상 정지)을 뺀 순수 사냥 시간 기준입니다.')
    hint.setWordWrap(True); layout.addWidget(hint)
    spots = QLabel(); spots.setWordWrap(True); spots.setStyleSheet('color:#e8d9a8;')
    layout.addWidget(spots)
    rows = collect()
    headers = ['날짜', 'Lv', '순수 사냥', '휴식', '획득', '분당', '시간당', '사냥터 메모']
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    best = max((row['per_minute'] for row in rows), default=0)
    for index, row in enumerate(rows):
        cells = [row['started'].strftime('%m/%d %H:%M') if row['started'] else row['session_id'],
                 str(row['level'] or '—'), clock(row['active']), clock(row['idle']) if row['idle'] >= 60 else '—',
                 short_exp(row['gained']), f"{row['per_minute']:,.0f}", short_exp(row['per_minute']*60), row['memo']]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column < 7:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column >= 4: item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            else:
                item.setData(Qt.ItemDataRole.UserRole, row['session_id'])
            if column == 5 and row['per_minute'] == best:
                item.setForeground(QColor('#8fe0a8'))
            table.setItem(index, column, item)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)

    def refresh_spots():
        ranked = by_memo(rows)
        spots.setText('사냥터별 분당 평균 · ' + ' / '.join(
            f'{memo} {rate:,.0f} ({clock(active)}, {count}회)' for memo, rate, active, count in ranked)
            if ranked else '사냥터 메모를 적으면 사냥터별 평균을 비교해 드려요.')

    def edited(item):
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if item.column() != 7 or not session_id: return
        try:
            text = save_memo(session_id, item.text())
        except OSError as exc:
            tracker.log_error('사냥터 메모 저장', exc); return
        for row in rows:
            if row['session_id'] == session_id: row['memo'] = text
        refresh_spots()

    table.itemChanged.connect(edited)
    refresh_spots()
    layout.addWidget(table)
    if not rows:
        layout.addWidget(QLabel('비교할 기록이 아직 없어요. (1분 이상 사냥한 기록만 표시)'))
    return dialog
