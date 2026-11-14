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
    if session is None or (session.started is None and not session.blocked):
        return None
    active, idle = session.active_elapsed(0.0), session.idle(0.0)
    gained = session.last[0]-session.first[0]
    if gained <= 0 and not session.blocked:
        return None
    rate = session.rate_estimate(0.0)
    try:
        started = datetime.strptime(csv_path.stem[:15], '%Y%m%d-%H%M%S')
    except ValueError:
        started = None
    import segments
    return dict(session_id=csv_path.stem, started=started, level=level, active=active, idle=idle,
                gained=gained, per_minute=rate['cumulative'], rate_gain=rate['rate_gain'],
                rate_seconds=rate['duration'], gap=session.gap_seconds(0.0), gap_gain=session.gap_gain,
                blocked=session.blocked, stop_reason=session.stop_reason,
                changes=segments.find_changes(session.minute_snapshots))


def collect(base=None):
    folder = Path(base or tracker.BASE)/'records'
    rows = []
    for path in sorted(folder.glob('[0-9]*.csv'), reverse=True):
        row = summarize(path)
        if row: rows.append(row)
    memos = load_memos(base)
    import segments
    tags = segments.load_tags(base)
    for row in rows:
        row['memo'] = memos.get(row['session_id'], '')
        for change in row['changes']:
            change['tag'] = tags.get(row['session_id'], {}).get(str(change['minute']), '')
    return rows


def by_memo(rows):
    """Weighted per-minute average per hunting spot (total EXP / total hunting time)."""
    groups = {}
    for row in comparable(rows):
        if not row['memo']: continue
        gained, active, count = groups.get(row['memo'], (0, 0.0, 0))
        groups[row['memo']] = (gained+row['rate_gain'], active+row['rate_seconds'], count+1)
    return sorted(((memo, gained*60/active, active, count) for memo, (gained, active, count) in groups.items()),
                  key=lambda item: -item[1])


def by_level(rows):
    """Weighted per-minute average per level (total EXP / total hunting time)."""
    groups = {}
    for row in comparable(rows):
        if row['level'] is None: continue
        gained, active = groups.get(row['level'], (0, 0.0))
        groups[row['level']] = (gained+row['rate_gain'], active+row['rate_seconds'])
    return [(level, gained*60/active) for level, (gained, active) in sorted(groups.items())]


def comparable(rows):
    return (row for row in rows if row['per_minute'] is not None and not row['blocked'])


def build_chart(rows):
    """Per-minute efficiency over time; hover a point for its details."""
    from PySide6.QtCore import Qt, QPointF, QRectF
    from PySide6.QtGui import QColor, QPainter, QPen, QPainterPath
    from PySide6.QtWidgets import QWidget, QToolTip
    from qt_controller import short_exp

    class EfficiencyChart(QWidget):
        LEFT, RIGHT, TOP, BOTTOM = 52, 12, 14, 24

        def __init__(self, points):
            super().__init__()
            self.points = points  # oldest first
            self.setMinimumHeight(170); self.setMouseTracking(True)
            total_gain = sum(r['rate_gain'] for r in points); total_time = sum(r['rate_seconds'] for r in points)
            self.average = total_gain*60/total_time if total_time else 0

        def plot(self):
            return QRectF(self.LEFT, self.TOP, self.width()-self.LEFT-self.RIGHT,
                          self.height()-self.TOP-self.BOTTOM)

        def scale(self):
            values = [r['per_minute'] for r in self.points]
            high = max(values)*1.1 if values else 1
            return 0.0, high or 1

        def position(self, index, value):
            area, (low, high) = self.plot(), self.scale()
            x = area.left()+(area.width()*index/(len(self.points)-1) if len(self.points) > 1 else area.width()/2)
            return QPointF(x, area.bottom()-(value-low)/(high-low)*area.height())

        def paintEvent(self, event):
            painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            area, (low, high) = self.plot(), self.scale()
            painter.fillRect(self.rect(), QColor('#1b2330'))
            painter.setPen(QColor('#84919f'))
            for step in range(5):
                value = low+(high-low)*step/4; y = area.bottom()-area.height()*step/4
                painter.setPen(QPen(QColor('#2b3542'), 1)); painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
                painter.setPen(QColor('#84919f'))
                painter.drawText(QRectF(0, y-8, self.LEFT-6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, short_exp(value))
            if not self.points:
                painter.drawText(area, Qt.AlignmentFlag.AlignCenter, '기록 없음'); return
            previous = None
            for index, row in enumerate(self.points):  # level changes and date labels
                x = self.position(index, 0).x()
                if previous is not None and row['level'] != previous:
                    painter.setPen(QPen(QColor('#3d4b5a'), 1, Qt.PenStyle.DotLine))
                    painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
                    painter.setPen(QColor('#9aa8b6')); painter.drawText(QPointF(x+3, area.top()+10), f'Lv.{row["level"]}')
                previous = row['level']
            labels = {0, len(self.points)-1, len(self.points)//2}
            painter.setPen(QColor('#84919f'))
            for index in sorted(labels):
                row = self.points[index]
                if row['started']:
                    x = self.position(index, 0).x()
                    box = (QRectF(x, area.bottom()+4, 60, 16), Qt.AlignmentFlag.AlignLeft) if index == 0 else \
                          (QRectF(x-60, area.bottom()+4, 60, 16), Qt.AlignmentFlag.AlignRight) if index == len(self.points)-1 else \
                          (QRectF(x-30, area.bottom()+4, 60, 16), Qt.AlignmentFlag.AlignCenter)
                    painter.drawText(box[0], box[1], row['started'].strftime('%m/%d'))
            if self.average:
                y = self.position(0, self.average).y()
                painter.setPen(QPen(QColor('#e8d9a8'), 1, Qt.PenStyle.DashLine))
                painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
                label = QRectF(area.left()+4, y-17, 130, 15)
                painter.fillRect(label, QColor(27, 35, 48, 220))
                painter.drawText(label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f'전체 평균 {self.average:,.0f}')
            path = QPainterPath()
            for index, row in enumerate(self.points):
                point = self.position(index, row['per_minute'])
                path.moveTo(point) if index == 0 else path.lineTo(point)
            painter.setPen(QPen(QColor('#8fe0a8'), 2)); painter.drawPath(path)
            for index, row in enumerate(self.points):
                point = self.position(index, row['per_minute'])
                short = row['active'] < 300  # under 5 minutes: noisy, draw faint
                painter.setBrush(QColor('#4f6b5a' if short else '#b4e9c6')); painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(point, 3 if short else 4, 3 if short else 4)

        def mouseMoveEvent(self, event):
            if not self.points: return
            x = event.position().x()
            index = min(range(len(self.points)), key=lambda i: abs(self.position(i, 0).x()-x))
            row = self.points[index]
            when = row['started'].strftime('%m/%d %H:%M') if row['started'] else row['session_id']
            memo = f" · {row['memo']}" if row.get('memo') else ''
            QToolTip.showText(event.globalPosition().toPoint(),
                f"{when} · Lv.{row['level']}{memo}\n분당 {row['per_minute']:,.0f} · 순수 사냥 {clock(row['active'])}", self)

    return EfficiencyChart(list(reversed(list(comparable(rows)))))


def change_text(changes):
    from qt_controller import short_exp
    return ' / '.join(f"{c['minute']}분~ {short_exp(c['before'])}→{short_exp(c['after'])}"
                      + (f" ({c['tag']})" if c.get('tag') else '') for c in changes) or '—'


def ratio(row):
    """Share of the measured time spent hunting (EXP rising), in percent."""
    total = row['active']+row['idle']
    return row['active']*100/total if total else 0.0


def clock(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds//3600}:{seconds%3600//60:02d}:{seconds%60:02d}'
    return f'{seconds//60}:{seconds%60:02d}'


def build_dialog(controller):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
                                   QHeaderView, QAbstractItemView)
    from qt_controller import short_exp
    dialog = QDialog(controller.window)
    dialog.setWindowTitle('사냥 기록 비교')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(900, 700)
    dialog.setStyleSheet('QDialog {background:#151b24;} QLabel {color:#d5dee8;}'
                         'QTableWidget {background:#1b2330;color:#d5dee8;gridline-color:#2b3542;}'
                         'QHeaderView::section {background:#25303d;color:#b8c4d0;border:none;padding:4px;}')
    layout = QVBoxLayout(dialog)
    hint = QLabel('분당 평균은 측정 화면과 같은 기준이에요. 인식 공백의 시간과 경험치는 효율에서 제외해요. 사냥터 메모는 두 번 눌러 편집할 수 있어요.')
    hint.setWordWrap(True); layout.addWidget(hint)
    spots = QLabel(); spots.setWordWrap(True); spots.setStyleSheet('color:#e8d9a8;')
    layout.addWidget(spots)
    rows = collect()
    layout.addWidget(build_chart(rows))
    levels = QLabel('레벨별 분당 평균 · ' + ' / '.join(f'Lv.{level} {rate:,.0f}' for level, rate in by_level(rows)))
    levels.setWordWrap(True); levels.setStyleSheet('color:#9aa8b6;'); layout.addWidget(levels)
    headers = ['날짜', 'Lv', '순수 사냥', '휴식', '인식 공백', '관측 중 사냥', '획득', '분당', '시간당', '효율 변화', '사냥터 메모']
    memo_column = len(headers)-1
    table = QTableWidget(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    best = max((row['per_minute'] for row in comparable(rows)), default=None)
    for index, row in enumerate(rows):
        rate = row['per_minute']
        warning = '확인 필요' if row['blocked'] else '관측 부족' if rate is None else ''
        cells = [row['started'].strftime('%m/%d %H:%M') if row['started'] else row['session_id'],
                 str(row['level'] or '—'), clock(row['active']), clock(row['idle']) if row['idle'] else '—',
                 clock(row['gap']) if row['gap'] else '—',
                 f"{ratio(row):.0f}%",
                 short_exp(row['gained']), warning or f"{rate:,.0f}", '—' if warning else short_exp(rate*60),
                 '—' if row['blocked'] else change_text(row['changes']), row['memo']]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column < memo_column:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column >= 4: item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            else:
                item.setData(Qt.ItemDataRole.UserRole, row['session_id'])
            if column == 4 and row['gap']:
                observed = row['active']+row['idle']
                coverage = observed/(observed+row['gap'])*100
                item.setToolTip(f"관측률 {coverage:.0f}% · 공백 사이 증가분 {row['gap_gain']:,.0f} EXP\n"
                                '총 획득에 포함, 효율에서 제외')
            if column == 5 and ratio(row) < 50:
                item.setForeground(QColor('#e7b86e'))
            if column == 7 and row['blocked']:
                item.setForeground(QColor('#e7b86e'))
                item.setToolTip(row['stop_reason'])
            if column == 7 and not warning and rate == best:
                item.setForeground(QColor('#8fe0a8'))
            table.setItem(index, column, item)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(memo_column, QHeaderView.ResizeMode.Stretch)

    def refresh_spots():
        ranked = by_memo(rows)
        spots.setText('사냥터별 분당 평균 · ' + ' / '.join(
            f'{memo} {rate:,.0f} ({clock(active)}, {count}회)' for memo, rate, active, count in ranked)
            if ranked else '사냥터 메모를 적으면 사냥터별 평균을 비교해 드려요.')

    def edited(item):
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if item.column() != memo_column or not session_id: return
        try:
            text = save_memo(session_id, item.text())
            import map_name
            if map_name.learn_correction(session_id, text):
                memos = load_memos()
                table.blockSignals(True)
                for index, other in enumerate(rows):
                    other['memo'] = memos.get(other['session_id'], '')
                    table.item(index, memo_column).setText(other['memo'])
                table.blockSignals(False)
        except OSError as exc:
            tracker.log_error('사냥터 메모 저장', exc); return
        for row in rows:
            if row['session_id'] == session_id: row['memo'] = text
        refresh_spots()

    table.itemChanged.connect(edited)
    refresh_spots()
    layout.addWidget(table)
    if not rows:
        layout.addWidget(QLabel('표시할 사냥 기록이 아직 없어요.'))
    return dialog
