"""Past measurements with hunting-spot memos, for comparing efficiency."""
import json
from datetime import datetime
from pathlib import Path

import exp_buffs
import tracker

MEMO_FILE = 'memos.json'
# Character names typed by the user. They override OCR without touching the record files.
CHARACTER_FILE = 'character-names.json'


def memo_path(base=None):
    return Path(base or tracker.BASE)/'records'/MEMO_FILE


def character_path(base=None):
    return Path(base or tracker.BASE)/'records'/CHARACTER_FILE


def _load_map(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_entries(path, entries, limit):
    """Set or clear several session ids in one atomic write; returns the cleaned texts."""
    data = _load_map(path)
    cleaned = {}
    for session_id, text in entries.items():
        text = ' '.join(str(text).split())[:limit]
        if text: data[session_id] = text
        else: data.pop(session_id, None)
        cleaned[session_id] = text
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return cleaned


def load_memos(base=None):
    return _load_map(memo_path(base))


def save_memo(session_id, text, base=None):
    return _save_entries(memo_path(base), {session_id: text}, 60)[session_id]


def save_memos(entries, base=None):
    return _save_entries(memo_path(base), entries, 60)


def load_character_names(base=None):
    return _load_map(character_path(base))


def save_character_names(entries, base=None):
    """An empty name removes the override, so the detected name (if any) shows again."""
    return _save_entries(character_path(base), entries, 24)


def summarize(csv_path):
    """One row per record; None when it has no usable hunting time."""
    csv_path = Path(csv_path)
    level = assigned = None
    try:
        summary = json.loads(csv_path.with_suffix('.json').read_text(encoding='utf-8'))
        level = summary.get('level')
        assigned = summary.get('character_assigned')
        assigned = assigned if isinstance(assigned, str) and assigned.strip() else None
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
                character_name=session.character_name or assigned, exp_buff_intervals=session.exp_buff_intervals,
                level_ups=len(session.level_ups),
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
    names = load_character_names(base)
    import segments
    tags = segments.load_tags(base)
    for row in rows:
        row['memo'] = memos.get(row['session_id'], '')
        row['detected_character'] = row['character_name']
        if row['session_id'] in names:
            row['character_name'] = names[row['session_id']]
        for change in row['changes']:
            change['tag'] = tags.get(row['session_id'], {}).get(str(change['minute']), '')
    return rows


def spot_key(memo):
    """Same spot regardless of spacing: '사자왕의성 · 낮은 성벽2' == '사자왕의 성 · 낮은 성벽2'."""
    return ''.join(str(memo).split()).replace('·', '').casefold()


def spot_spellings(memos):
    """For each spacing-insensitive key, the spelling used most often (latest wins ties)."""
    counts, latest = {}, {}
    for order, memo in enumerate(memos):
        if not memo: continue
        key = spot_key(memo)
        counts.setdefault(key, {}).setdefault(memo, 0)
        counts[key][memo] += 1
        latest.setdefault(key, {})[memo] = -order
    return {key: max(spellings, key=lambda memo: (spellings[memo], latest[key][memo]))
            for key, spellings in counts.items()}


def by_memo(rows):
    """Weighted per-minute average per hunting spot (total EXP / total hunting time)."""
    rows = list(comparable(rows))
    names = spot_spellings(row['memo'] for row in rows)
    groups = {}
    for row in rows:
        if not row['memo']: continue
        name = names[spot_key(row['memo'])]
        gained, active, count = groups.get(name, (0, 0.0, 0))
        groups[name] = (gained+row['rate_gain'], active+row['rate_seconds'], count+1)
    return sorted(((memo, gained*60/active, active, count) for memo, (gained, active, count) in groups.items()),
                  key=lambda item: -item[1])


def known_characters(base=None):
    """Names seen in records: typed, assigned, or read by OCR."""
    names = set(load_character_names(base).values())
    for path in (Path(base or tracker.BASE)/'records').glob('[0-9]*.json'):
        try:
            summary = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(summary, dict): continue
        for key in ('character_name', 'character_assigned'):
            if isinstance(summary.get(key), str) and summary[key].strip():
                names.add(summary[key])
    return sorted(names)


def period_summary(rows, since):
    """Totals for records that started at or after `since` (a datetime)."""
    chosen = [row for row in rows if row['started'] and row['started'] >= since]
    rated = [row for row in comparable(chosen)]
    active = sum(row['active'] for row in chosen)
    rate_gain = sum(row['rate_gain'] for row in rated)
    rate_seconds = sum(row['rate_seconds'] for row in rated)
    spots = by_memo(chosen)
    characters = {}
    for row in chosen:
        name = row['character_name'] or '캐릭터 미확인'
        characters[name] = characters.get(name, 0) + row['gained']
    return dict(count=len(chosen), active=active, gained=sum(row['gained'] for row in chosen),
                level_ups=sum(row.get('level_ups', 0) for row in chosen),
                per_minute=rate_gain*60/rate_seconds if rate_seconds else None,
                best_spot=spots[0] if spots else None,
                characters=sorted(characters.items(), key=lambda item: -item[1]))


def period_text(rows, now=None):
    from datetime import timedelta
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    lines = []
    for title, since in (('오늘', today), ('이번 주 (월요일부터)', week)):
        summary = period_summary(rows, since)
        if not summary['count']:
            lines.append(f'{title} · 사냥 기록 없음'); continue
        lines.append(f"{title} · {summary['count']}회 · 순수 사냥 {clock(summary['active'])} · "
                     f"획득 {summary['gained']:,} EXP")
        details = []
        if summary['per_minute'] is not None: details.append(f"분당 평균 {summary['per_minute']:,.0f}")
        if summary['level_ups']: details.append(f"레벨업 {summary['level_ups']}회")
        if summary['best_spot']:
            spot, rate, _, count = summary['best_spot']
            details.append(f'최고 효율 {spot} (분당 {rate:,.0f}, {count}회)')
        if details: lines.append('    ' + ' · '.join(details))
        if len(summary['characters']) > 1 or summary['characters'][0][0] != '캐릭터 미확인':
            lines.append('    ' + ' / '.join(f'{name} {gained:,}' for name, gained in summary['characters']))
    return '\n'.join(lines)


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


def buff_cell(intervals):
    lines = []
    labels = []
    for key, label in exp_buffs.LABELS.items():
        spans = intervals.get(key)
        if not isinstance(spans, list):
            continue
        times = [f'{clock(start)}–{clock(end)}' for span in spans
                 if isinstance(span, list) and len(span) == 2
                 for start, end in (span,)
                 if all(isinstance(t, (int, float)) and 0 <= t < 10**8 for t in span) and start <= end]
        if times:
            labels.append(label)
            lines.append(f'{label} ' + ', '.join(times))
    return ' · '.join(labels) or '—', '\n'.join(lines)


def build_dialog(controller):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
                                   QHeaderView, QAbstractItemView, QComboBox, QHBoxLayout,
                                   QInputDialog, QMenu)
    from qt_controller import short_exp
    dialog = QDialog(controller.window)
    dialog.setWindowTitle('사냥 기록 비교')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(900, 700)
    dialog.setStyleSheet('QDialog {background:#151b24;} QLabel {color:#d5dee8;}'
                         'QTableWidget {background:#1b2330;color:#d5dee8;gridline-color:#2b3542;}'
                         'QHeaderView::section {background:#25303d;color:#b8c4d0;border:none;padding:4px;}')
    layout = QVBoxLayout(dialog)
    hint = QLabel('분당 평균은 측정 화면과 같은 기준이에요. 인식 공백의 시간과 경험치는 효율에서 제외해요. '
                  '캐릭터와 사냥터 메모는 두 번 눌러 고칠 수 있고, 여러 줄을 고른 뒤 오른쪽 클릭하면 한꺼번에 바꿀 수 있어요.')
    hint.setWordWrap(True); layout.addWidget(hint)
    spots = QLabel(); spots.setWordWrap(True); spots.setStyleSheet('color:#e8d9a8;')
    all_rows = collect()
    character_names = []
    character_picker = QComboBox()
    character_picker.setAccessibleName('사냥 기록 캐릭터')

    def populate_picker(selected):
        """Rebuild the filter after a rename; keep the chosen character if it still exists."""
        nonlocal character_names
        character_names = sorted({row['character_name'] for row in all_rows if row['character_name']})
        character_picker.blockSignals(True)
        character_picker.clear()
        character_picker.addItem('전체 캐릭터', None)
        for name in character_names:
            character_picker.addItem(name, name)
        if any(not row['character_name'] for row in all_rows):
            character_picker.addItem('캐릭터 미확인', '')
        index = character_picker.findData(selected)
        character_picker.setCurrentIndex(index if index >= 0 else 0)
        character_picker.blockSignals(False)

    populate_picker(None)
    period = QLabel(period_text(all_rows))
    period.setStyleSheet('color:#a8d8b9;')
    period.setAccessibleName('오늘·이번 주 요약')
    layout.addWidget(period)
    filter_row = QHBoxLayout()
    filter_row.addWidget(QLabel('캐릭터'))
    filter_row.addWidget(character_picker, 1)
    layout.addLayout(filter_row)
    layout.addWidget(spots)
    chart_layout = QVBoxLayout()
    layout.addLayout(chart_layout)
    levels = QLabel()
    levels.setWordWrap(True); levels.setStyleSheet('color:#9aa8b6;'); layout.addWidget(levels)
    headers = ['날짜', '캐릭터', 'Lv', '순수 사냥', '휴식', '인식 공백', '관측 중 사냥', '획득', '분당', '시간당', '효율 변화', '경험치 버프', '사냥터 메모']
    memo_column = len(headers)-1
    character_column = 1
    editable_columns = (character_column, memo_column)
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(memo_column, QHeaderView.ResizeMode.Stretch)

    def refresh_spots():
        ranked = by_memo(rows)
        spots.setText('사냥터별 분당 평균 · ' + ' / '.join(
            f'{memo} {rate:,.0f} ({clock(active)}, {count}회)' for memo, rate, active, count in ranked)
            if ranked else '사냥터 메모를 적으면 사냥터별 평균을 비교해 드려요.')

    def rename_characters(session_ids, name):
        try:
            save_character_names({session_id: name for session_id in session_ids})
        except OSError as exc:
            tracker.log_error('캐릭터 이름 저장', exc); return
        overrides = load_character_names()
        for row in all_rows:
            if row['session_id'] in session_ids:
                row['character_name'] = overrides.get(row['session_id'], row['detected_character'])
        # A renamed record may leave the filtered view, so rebuild both.
        populate_picker(character_picker.currentData())
        render_records()

    def rename_spots(session_ids, text):
        try:
            save_memos({session_id: text for session_id in session_ids})
            import map_name
            for session_id in session_ids:
                map_name.learn_correction(session_id, ' '.join(str(text).split())[:60])
        except OSError as exc:
            tracker.log_error('사냥터 메모 저장', exc); return
        # learn_correction may also fix other records with the same OCR reading.
        memos = load_memos()
        for row in all_rows:
            row['memo'] = memos.get(row['session_id'], '')
        table.blockSignals(True)
        for index, row in enumerate(rows):
            table.item(index, memo_column).setText(row['memo'])
        table.blockSignals(False)
        refresh_spots()

    def edited(item):
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if item.column() not in editable_columns or not session_id: return
        if item.column() == character_column:
            name = '' if item.text().strip() == '—' else item.text()
            # Defer: rebuilding the table inside its own itemChanged signal is unsafe.
            QTimer.singleShot(0, lambda: rename_characters({session_id}, name))
        else:
            rename_spots({session_id}, item.text())

    def selected_ids():
        indexes = {index.row() for index in table.selectionModel().selectedRows()}
        return {rows[index]['session_id'] for index in sorted(indexes) if index < len(rows)}

    def ask_name(title, label, choices, current):
        options = [current] + [choice for choice in choices if choice != current] if current else list(choices)
        text, accepted = QInputDialog.getItem(dialog, title, label, options or [''], 0, True)
        return text if accepted else None

    def context_menu(position):
        clicked = table.indexAt(position)
        if clicked.isValid() and not table.selectionModel().isRowSelected(clicked.row(), clicked.parent()):
            table.selectRow(clicked.row())
        session_ids = selected_ids()
        if not session_ids: return
        count = len(session_ids)
        chosen = [row for row in rows if row['session_id'] in session_ids]
        menu = QMenu(table)
        character_action = menu.addAction(f'캐릭터 이름 바꾸기 ({count}개 기록)…')
        spot_action = menu.addAction(f'사냥터 메모 바꾸기 ({count}개 기록)…')
        reset_action = None
        if any(row['character_name'] != row['detected_character'] for row in chosen):
            reset_action = menu.addAction('캐릭터 이름을 자동 인식 값으로 되돌리기')
        action = menu.exec(table.viewport().mapToGlobal(position))
        if action is character_action:
            current = chosen[0]['character_name'] or ''
            name = ask_name('캐릭터 이름', f'{count}개 기록의 캐릭터 이름 (비우면 자동 인식 값)',
                            character_names, current)
            if name is not None: rename_characters(session_ids, name)
        elif action is spot_action:
            current = chosen[0]['memo']
            spots_known = sorted({row['memo'] for row in all_rows if row['memo']})
            text = ask_name('사냥터 메모', f'{count}개 기록의 사냥터', spots_known, current)
            if text is not None: rename_spots(session_ids, text)
        elif reset_action is not None and action is reset_action:
            rename_characters(session_ids, '')

    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    table.customContextMenuRequested.connect(context_menu)

    rows = []
    empty = QLabel('표시할 사냥 기록이 아직 없어요.')

    def render_records():
        nonlocal rows
        name = character_picker.currentData()
        rows = [row for row in all_rows if name is None or (row['character_name'] or '') == name]
        if chart_layout.count():
            chart_layout.takeAt(0).widget().deleteLater()
        chart_layout.addWidget(build_chart(rows))
        levels.setText('레벨별 분당 평균 · ' + ' / '.join(
            f'Lv.{level} {rate:,.0f}' for level, rate in by_level(rows)))
        table.blockSignals(True)
        table.clearContents()
        table.setRowCount(len(rows))
        best = max((row['per_minute'] for row in comparable(rows)), default=None)
        for index, row in enumerate(rows):
            rate = row['per_minute']
            warning = '확인 필요' if row['blocked'] else '관측 부족' if rate is None else ''
            buffs, buff_times = buff_cell(row['exp_buff_intervals'])
            cells = [row['started'].strftime('%m/%d %H:%M') if row['started'] else row['session_id'],
                     row['character_name'] or '—', str(row['level'] or '—'), clock(row['active']), clock(row['idle']) if row['idle'] else '—',
                     clock(row['gap']) if row['gap'] else '—',
                     f"{ratio(row):.0f}%",
                     short_exp(row['gained']), warning or f"{rate:,.0f}", '—' if warning else short_exp(rate*60),
                     '—' if row['blocked'] else change_text(row['changes']), buffs, row['memo']]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column not in editable_columns:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column >= 5: item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setData(Qt.ItemDataRole.UserRole, row['session_id'])
                if column == character_column:
                    detected = row['detected_character']
                    if row['character_name'] != detected:
                        item.setToolTip(f"직접 고친 이름 · 자동 인식: {detected or '없음'}")
                    else:
                        item.setToolTip('두 번 눌러 고치기' if detected else '캐릭터 미확인 · 두 번 눌러 이름 적기')
                if column == memo_column - 1:
                    item.setToolTip(buff_times or '확인된 버프 구간 없음')
                if column == 5 and row['gap']:
                    observed = row['active']+row['idle']
                    coverage = observed/(observed+row['gap'])*100
                    item.setToolTip(f"관측률 {coverage:.0f}% · 공백 사이 증가분 {row['gap_gain']:,.0f} EXP\n"
                                    '총 획득에 포함, 효율에서 제외')
                if column == 6 and ratio(row) < 50:
                    item.setForeground(QColor('#e7b86e'))
                if column == 8 and row['blocked']:
                    item.setForeground(QColor('#e7b86e'))
                    item.setToolTip(row['stop_reason'])
                if column == 8 and not warning and rate == best:
                    item.setForeground(QColor('#8fe0a8'))
                table.setItem(index, column, item)
        table.blockSignals(False)
        empty.setVisible(not rows)
        refresh_spots()

    table.itemChanged.connect(edited)
    character_picker.currentIndexChanged.connect(render_records)
    layout.addWidget(table)
    layout.addWidget(empty)
    session_name = controller.session.character_name or getattr(controller, 'current_character', None)
    if session_name in character_names:
        character_picker.setCurrentIndex(character_picker.findData(session_name))
    else:
        render_records()
    return dialog
