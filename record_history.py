"""Past measurements with hunting-spot memos, for comparing efficiency."""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import exp_buffs
import tracker

MEMO_FILE = 'memos.json'
# Character names typed by the user. They override OCR without touching the record files.
CHARACTER_FILE = 'character-names.json'
CACHE_FILE = '.summary-cache.json'
CACHE_VERSION = 1
_cache_lock = threading.Lock()
_memory_cache = {}


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
                level_ups=len(session.level_ups), meso=session.meso.hunted(), meso_trade=session.meso.excluded_trade(),
                meso_large_changes=len(session.meso.large_changes),
                gained=gained, per_minute=rate['cumulative'], rate_gain=rate['rate_gain'],
                rate_seconds=rate['duration'], gap=session.gap_seconds(0.0), gap_gain=session.gap_gain,
                blocked=session.blocked, stop_reason=session.stop_reason,
                changes=segments.find_changes(session.minute_snapshots))


def _record_stamp(csv_path):
    """Return [csv_mtime_ns, csv_size, json_mtime_ns, json_size] for cache validation."""
    csv_stat = csv_path.stat()
    json_path = csv_path.with_suffix('.json')
    try:
        json_stat = json_path.stat()
        return [csv_stat.st_mtime_ns, csv_stat.st_size, json_stat.st_mtime_ns, json_stat.st_size]
    except OSError:
        return [csv_stat.st_mtime_ns, csv_stat.st_size, 0, 0]


def _encode_cached_row(row):
    if row is None:
        return None
    encoded = dict(row)
    started = encoded.get('started')
    encoded['started'] = started.isoformat() if isinstance(started, datetime) else None
    return encoded


def _decode_cached_row(entry):
    if entry is None:
        return None
    row = dict(entry)
    started = row.get('started')
    if isinstance(started, str):
        try:
            row['started'] = datetime.fromisoformat(started)
        except ValueError:
            row['started'] = None
    row['changes'] = [dict(change) for change in row.get('changes') or ()]
    row['exp_buff_intervals'] = {
        key: [list(span) for span in spans]
        for key, spans in (row.get('exp_buff_intervals') or {}).items()
        if isinstance(spans, list)
    }
    return row


def _load_summary_cache(folder):
    key = str(folder.resolve()) if folder.exists() else str(folder)
    with _cache_lock:
        cached = _memory_cache.get(key)
        if cached is not None:
            return dict(cached)
    path = folder / CACHE_FILE
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(payload, dict) and payload.get('version') == CACHE_VERSION and isinstance(payload.get('entries'), dict):
            entries = payload['entries']
            with _cache_lock:
                _memory_cache[key] = dict(entries)
            return dict(entries)
    except (OSError, ValueError):
        pass
    return {}


def _save_summary_cache(folder, entries):
    key = str(folder.resolve()) if folder.exists() else str(folder)
    with _cache_lock:
        _memory_cache[key] = dict(entries)
    path = folder / CACHE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps({'version': CACHE_VERSION, 'entries': entries}, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        pass


def collect(base=None):
    folder = Path(base or tracker.BASE)/'records'
    rows = []
    cache = _load_summary_cache(folder) if folder.is_dir() else {}
    dirty = False
    seen = set()
    if folder.is_dir():
        for path in sorted(folder.glob('[0-9]*.csv'), reverse=True):
            session_id = path.stem
            seen.add(session_id)
            try:
                stamp = _record_stamp(path)
            except OSError:
                continue
            cached = cache.get(session_id)
            if isinstance(cached, dict) and cached.get('stamp') == stamp:
                row = _decode_cached_row(cached.get('row'))
            else:
                row = summarize(path)
                cache[session_id] = {'stamp': stamp, 'row': _encode_cached_row(row)}
                dirty = True
            if row:
                rows.append(row)
        stale = [sid for sid in cache if sid not in seen]
        if stale:
            for sid in stale:
                cache.pop(sid, None)
            dirty = True
        if dirty:
            _save_summary_cache(folder, cache)
    memos = load_memos(base)
    names = load_character_names(base)
    import segments
    import jev_assist
    tags = segments.load_tags(base)
    stalls = jev_assist.load_stalls(base)
    for row in rows:
        row['stalls'] = stalls.get(row['session_id'], {})
        row['memo'] = memos.get(row['session_id'], '')
        row['detected_character'] = row['character_name']
        if row['session_id'] in names:
            row['character_name'] = names[row['session_id']]
        for change in row['changes']:
            change['tag'] = tags.get(row['session_id'], {}).get(str(change['minute']), '')
    return rows


def filter_rows(all_rows, *, character=None, period=None, query='', now=None):
    """Filter collected rows by character, time window, and search text."""
    since = None
    if period in ('today', '7d', '30d'):
        current = now or datetime.now()
        today = current.replace(hour=0, minute=0, second=0, microsecond=0)
        since = today if period == 'today' else today - timedelta(days=6 if period == '7d' else 29)
    needle = ' '.join(str(query or '').split()).casefold()
    compact_needle = spot_key(needle) if needle else ''
    result = []
    for row in all_rows:
        if character is not None and (row.get('character_name') or '') != character:
            continue
        if since is not None:
            started = row.get('started')
            if started is None or started < since:
                continue
        if needle:
            memo = row.get('memo') or ''
            char = row.get('character_name') or ''
            level = f"lv.{row['level']}" if row.get('level') is not None else ''
            haystack = f'{memo} {char} {level}'.casefold()
            if needle not in haystack and (not compact_needle or compact_needle not in spot_key(memo)):
                continue
        result.append(row)
    return result


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


def by_memo_detailed(rows):
    """Weighted per-minute EXP and hourly meso per hunting spot."""
    rows = list(comparable(rows))
    names = spot_spellings(row['memo'] for row in rows)
    groups = {}
    for row in rows:
        if not row['memo']: continue
        name = names[spot_key(row['memo'])]
        gained, active, count, meso_sum, meso_sec, meso_count = groups.get(name, (0, 0.0, 0, 0, 0.0, 0))
        meso_val = row.get('meso')
        if meso_val is not None and row.get('active', 0) > 0:
            meso_sum += meso_val
            meso_sec += row['active']
            meso_count += 1
        groups[name] = (gained+row['rate_gain'], active+row['rate_seconds'], count+1,
                        meso_sum, meso_sec, meso_count)
    return sorted((
        (memo, gained*60/active, active, count,
         (meso_sum*3600/meso_sec if meso_sec > 0 else None), meso_count)
        for memo, (gained, active, count, meso_sum, meso_sec, meso_count) in groups.items()
    ), key=lambda item: -item[1])


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
            previous, last_label = None, -1e9
            for index, row in enumerate(self.points):  # level changes and date labels
                x = self.position(index, 0).x()
                # Skip level markers that would overlap the last one drawn.
                if previous is not None and row['level'] != previous and x-last_label >= 56:
                    painter.setPen(QPen(QColor('#3d4b5a'), 1, Qt.PenStyle.DotLine))
                    painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
                    painter.setPen(QColor('#9aa8b6')); painter.drawText(QPointF(x+3, area.top()+10), f'Lv.{row["level"]}')
                    last_label = x
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


STYLE = '''
QDialog {background:#151b24;} QLabel {color:#d5dee8;}
QFrame#card {background:#1b2330; border-radius:10px;}
QLabel#cardTitle, QLabel#hint, QLabel#statName {color:#84919f;}
QLabel#statMain {color:#8fe0a8; font-size:22px; font-weight:600;}
QLabel#stat {font-size:16px; font-weight:600;}
QLabel#statName {font-size:11px;}
QLabel#best {color:#e8d9a8;}
QLabel#title {font-size:17px; font-weight:600;}
QTabWidget::pane {border:none; border-top:1px solid #2b3542;}
QTabBar {background:transparent;} QTabBar::tab {background:transparent; color:#84919f; padding:7px 16px; border:none;}
QTabBar::tab:selected {color:#e8f0f6; border-bottom:2px solid #8fe0a8;}
QTableWidget {background:#1b2330; alternate-background-color:#18202b; color:#d5dee8;
              gridline-color:#232c38; border:none; border-radius:10px;}
QTableWidget::item {padding:4px 6px;}
QTableWidget::item:selected {background:#2d4a63;}
QHeaderView::section {background:#25303d; color:#b8c4d0; border:none; padding:6px;}
'''


def stat_card(title):
    """A summary card; returns (frame, update(summary, label)) for the filtered rows."""
    from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QVBoxLayout
    from qt_controller import short_exp
    frame = QFrame(); frame.setObjectName('card')
    layout = QVBoxLayout(frame); layout.setContentsMargins(14, 10, 14, 10); layout.setSpacing(4)
    heading = QLabel(); heading.setObjectName('cardTitle'); layout.addWidget(heading)
    grid = QGridLayout(); grid.setHorizontalSpacing(24); grid.setVerticalSpacing(0)
    values = []
    for column, name in enumerate(('획득 EXP', '분당 평균', '순수 사냥', '레벨업')):
        value = QLabel(); value.setObjectName('statMain' if column == 0 else 'stat')
        caption = QLabel(name); caption.setObjectName('statName')
        grid.addWidget(value, 0, column); grid.addWidget(caption, 1, column); values.append(value)
    grid.setColumnStretch(4, 1)
    layout.addLayout(grid)
    best = QLabel(); best.setObjectName('best'); layout.addWidget(best)

    def update(summary):
        heading.setText(f"{title} · {summary['count']}회" if summary['count'] else f'{title} · 사냥 기록 없음')
        rate = summary['per_minute']
        texts = (short_exp(summary['gained']) if summary['count'] else '—',
                 f'{rate:,.0f}' if rate is not None else '—',
                 clock(summary['active']) if summary['count'] else '—',
                 f"{summary['level_ups']}회" if summary['count'] else '—')
        for label, text in zip(values, texts): label.setText(text)
        values[0].setToolTip(f"{summary['gained']:,} EXP")
        if summary['best_spot']:
            spot, spot_rate, _, _ = summary['best_spot']
            best.setText(f'🏆 최고 효율 · {spot} (분당 {spot_rate:,.0f})')
        else:
            best.setText('사냥터 메모를 적으면 최고 효율 사냥터를 알려 드려요.')
        if len(summary['characters']) > 1:
            best.setToolTip(' / '.join(f'{name} {gained:,}' for name, gained in summary['characters']))
    return frame, update


def ranking_table(first_header):
    """Rank / name / per-minute bar / hunting time / count."""
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QTableWidget, QHeaderView, QAbstractItemView, QStyledItemDelegate)

    class BarDelegate(QStyledItemDelegate):
        def paint(self, painter, option, index):
            share = index.data(Qt.ItemDataRole.UserRole) or 0
            box = QRectF(option.rect).adjusted(4, 6, -8, -6)
            painter.save()
            painter.fillRect(QRectF(box.left(), box.top(), box.width()*share, box.height()), QColor('#2a4a38'))
            painter.setPen(QColor('#d5dee8'))
            painter.drawText(box.adjusted(6, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             index.data())
            painter.restore()

    table = QTableWidget(0, 5)
    table.setHorizontalHeaderLabels(['#', first_header, '분당 평균', '순수 사냥', '횟수'])
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True); table.setShowGrid(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setItemDelegateForColumn(2, BarDelegate(table))
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    return table


def spot_ranking_table():
    """Rank / spot / per-minute EXP bar / hourly meso / hunting time / count."""
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QTableWidget, QHeaderView, QAbstractItemView, QStyledItemDelegate)

    class BarDelegate(QStyledItemDelegate):
        def paint(self, painter, option, index):
            share = index.data(Qt.ItemDataRole.UserRole) or 0
            box = QRectF(option.rect).adjusted(4, 6, -8, -6)
            painter.save()
            painter.fillRect(QRectF(box.left(), box.top(), box.width()*share, box.height()), QColor('#2a4a38'))
            painter.setPen(QColor('#d5dee8'))
            painter.drawText(box.adjusted(6, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             index.data())
            painter.restore()

    table = QTableWidget(0, 6)
    table.setHorizontalHeaderLabels(['#', '사냥터', '분당 EXP', '시간당 메소', '순수 사냥', '횟수'])
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True); table.setShowGrid(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setItemDelegateForColumn(2, BarDelegate(table))
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    return table


def fill_spot_ranking(table, entries):
    """entries: (name, per_minute, active, count, meso_per_hour, meso_count)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem
    top = max((rate for _, rate, _, _, _, _ in entries), default=0) or 1
    table.setRowCount(len(entries))
    for index, (name, rate, active, count, meso_hour, meso_count) in enumerate(entries):
        meso_text = '—' if meso_hour is None else f'{meso_hour:+,.0f}'
        cells = [str(index+1), name, f'{rate:,.0f}', meso_text, clock(active), str(count)]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 2:
                item.setData(Qt.ItemDataRole.UserRole, rate/top)
            if column == 3 and meso_hour is not None:
                item.setToolTip(f'메소 확인 기록 {meso_count}건 가중 평균 (시간당 {meso_hour:+,.0f} 메소)')
            if column in (0, 3, 4, 5):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(index, column, item)


def fill_ranking(table, entries):
    """entries: (name, per_minute, active seconds, count), best first."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem
    top = max((rate for _, rate, _, _ in entries), default=0) or 1
    table.setRowCount(len(entries))
    for index, (name, rate, active, count) in enumerate(entries):
        cells = [str(index+1), name, f'{rate:,.0f}', clock(active), str(count)]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 2: item.setData(Qt.ItemDataRole.UserRole, rate/top)
            if column in (0, 3, 4): item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(index, column, item)


def level_ranking(rows):
    """Like by_memo, but per level, highest level first."""
    groups = {}
    for row in comparable(rows):
        if row['level'] is None: continue
        gained, active, count = groups.get(row['level'], (0, 0.0, 0))
        groups[row['level']] = (gained+row['rate_gain'], active+row['rate_seconds'], count+1)
    return [(f'Lv.{level}', gained*60/active, active, count)
            for level, (gained, active, count) in sorted(groups.items(), reverse=True) if active]


def meso_cell(row):
    return '—' if row.get('meso') is None else f"{row['meso']:+,}"


def meso_detail(row):
    if row.get('meso') is None:
        return '메소 · 같은 캐릭터와 게임 창에서 시작·종료 잔액 확인 안 됨'
    trade = row.get('meso_trade') or 0
    per_minute = row['meso']*60/row['active'] if row['active'] else None
    text = f"메소 {row['meso']:+,}" + (f' · 분당 {per_minute:,.0f}' if per_minute is not None else '')
    large = row.get('meso_large_changes') or 0
    if large:
        text += f' · 큰 변동 {large}건'
    return text + (f' (거래·구매 {trade:+,} 제외)' if trade else '')


def row_details(row, buff_times):
    """Tooltip with the columns the table no longer shows."""
    from qt_controller import short_exp
    rate = row['per_minute']
    lines = [f"휴식 {clock(row['idle']) if row['idle'] else '없음'} · 인식 공백 {clock(row['gap']) if row['gap'] else '없음'}"]
    if row['gap']:
        observed = row['active']+row['idle']
        lines.append(f"관측률 {observed/(observed+row['gap'])*100:.0f}% · 공백 사이 증가분 {row['gap_gain']:,.0f} EXP"
                     ' (총 획득에 포함, 효율에서 제외)')
    if rate is not None and not row['blocked']:
        lines.append(f'시간당 {short_exp(rate*60)}')
    if row['blocked']:
        lines.append(f"확인 필요 · {row['stop_reason']}")
    else:
        lines.append(f"효율 변화 {change_text(row['changes'])}")
    lines.append(f"경험치 버프 {buff_times.replace(chr(10), ' / ') or '확인된 구간 없음'}")
    lines.append(meso_detail(row))
    if row.get('stalls'):
        import jev_assist
        lines.append(f"경험치가 멈춘 이유 (JEV) {jev_assist.stall_text(row['stalls'])}")
    return '\n'.join(lines)


def build_dialog(controller):
    from datetime import timedelta
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
                                   QHeaderView, QAbstractItemView, QComboBox, QHBoxLayout,
                                   QInputDialog, QLineEdit, QMenu, QTabWidget, QWidget)
    from qt_controller import short_exp
    dialog = QDialog(controller.window)
    dialog.setWindowTitle('사냥 기록 비교')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.resize(1040, 760)
    dialog.setStyleSheet(STYLE + 'QLineEdit, QComboBox {background:#25303d; color:#e8f0f6; border:1px solid #35465b; border-radius:6px; padding:4px 8px;}')
    layout = QVBoxLayout(dialog); layout.setContentsMargins(16, 14, 16, 12); layout.setSpacing(10)
    all_rows = collect()
    character_names = []
    character_picker = QComboBox()
    character_picker.setAccessibleName('사냥 기록 캐릭터')
    period_picker = QComboBox()
    period_picker.setAccessibleName('사냥 기록 기간')
    for label, value in (('전체 기간', None), ('오늘', 'today'), ('최근 7일', '7d'), ('최근 30일', '30d')):
        period_picker.addItem(label, value)
    search_input = QLineEdit()
    search_input.setAccessibleName('사냥 기록 검색')
    search_input.setPlaceholderText('사냥터·레벨·캐릭터 검색…')
    search_input.setClearButtonEnabled(True)
    search_input.setMinimumWidth(180)

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
    top = QHBoxLayout()
    title = QLabel('사냥 기록'); title.setObjectName('title'); top.addWidget(title)
    top.addStretch(1)
    top.addWidget(search_input)
    caption = QLabel('캐릭터'); caption.setObjectName('hint'); top.addWidget(caption)
    character_picker.setMinimumWidth(160); top.addWidget(character_picker)
    period_caption = QLabel('기간'); period_caption.setObjectName('hint'); top.addWidget(period_caption)
    top.addWidget(period_picker)
    layout.addLayout(top)

    cards = QHBoxLayout(); cards.setSpacing(12)
    today_card, update_today = stat_card('오늘')
    week_card, update_week = stat_card('이번 주 (월요일부터)')
    for card in (today_card, week_card): cards.addWidget(card, 1)
    for card in (today_card, week_card): card.setAccessibleName('오늘·이번 주 요약')
    layout.addLayout(cards)

    tabs = QTabWidget(); tabs.setDocumentMode(True); tabs.tabBar().setDrawBase(False)
    records_page = QWidget(); records_layout = QVBoxLayout(records_page)
    records_layout.setContentsMargins(0, 10, 0, 0); records_layout.setSpacing(8)
    chart_layout = QVBoxLayout(); records_layout.addLayout(chart_layout)
    headers = ['날짜', '캐릭터', 'Lv', '순수 사냥', '획득', '분당', '관측 중 사냥', '메소', '사냥터 메모']
    memo_column = len(headers)-1
    character_column = 1
    rate_column, ratio_column = 5, 6
    editable_columns = (character_column, memo_column)
    table = QTableWidget(0, len(headers)); table.setObjectName('records')
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True); table.setShowGrid(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(memo_column, QHeaderView.ResizeMode.Stretch)
    records_layout.addWidget(table, 1)
    empty = QLabel('표시할 사냥 기록이 아직 없어요.'); empty.setObjectName('hint')
    records_layout.addWidget(empty)
    hint = QLabel('ⓘ 줄에 마우스를 올리면 휴식 · 인식 공백 · 시간당 · 효율 변화 · 경험치 버프가 보여요. '
                  '캐릭터와 사냥터 메모는 두 번 눌러 고치고, 여러 줄을 고른 뒤 오른쪽 클릭하면 한꺼번에 바꿔요. '
                  '인식 공백의 시간과 경험치는 효율에서 제외해요.')
    hint.setObjectName('hint'); hint.setWordWrap(True); records_layout.addWidget(hint)
    tabs.addTab(records_page, '기록')

    spot_table = spot_ranking_table()
    spot_page = QWidget(); spot_layout = QVBoxLayout(spot_page); spot_layout.setContentsMargins(0, 10, 0, 0)
    spot_layout.addWidget(spot_table, 1)
    spot_hint = QLabel('띄어쓰기만 다른 사냥터 이름은 하나로 묶어요. 사냥터 메모를 적은 기록만 나와요.')
    spot_hint.setObjectName('hint'); spot_layout.addWidget(spot_hint)
    tabs.addTab(spot_page, '사냥터별')
    level_table = ranking_table('레벨'); level_table.setColumnHidden(0, True)
    level_page = QWidget(); level_layout = QVBoxLayout(level_page); level_layout.setContentsMargins(0, 10, 0, 0)
    level_layout.addWidget(level_table, 1)
    tabs.addTab(level_page, '레벨별')
    layout.addWidget(tabs, 1)

    def refresh_spots():
        ranked = by_memo_detailed(rows)
        fill_spot_ranking(spot_table, ranked)
        tabs.setTabText(1, f'사냥터별 {len(ranked)}')

    def refresh_summary():
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        name = character_picker.currentData()
        char_rows = [row for row in all_rows if name is None or (row['character_name'] or '') == name]
        update_today(period_summary(char_rows, today))
        update_week(period_summary(char_rows, today-timedelta(days=today.weekday())))

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
        refresh_spots(); refresh_summary()

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

    def render_records():
        nonlocal rows
        rows = filter_rows(all_rows, character=character_picker.currentData(),
                           period=period_picker.currentData(), query=search_input.text())
        if chart_layout.count():
            chart_layout.takeAt(0).widget().deleteLater()
        chart_layout.addWidget(build_chart(rows))
        levels = level_ranking(rows)
        fill_ranking(level_table, levels)
        tabs.setTabText(0, f'기록 {len(rows)}')
        tabs.setTabText(2, f'레벨별 {len(levels)}')
        table.blockSignals(True)
        table.clearContents()
        table.setRowCount(len(rows))
        best = max((row['per_minute'] for row in comparable(rows)), default=None)
        for index, row in enumerate(rows):
            rate = row['per_minute']
            warning = '확인 필요' if row['blocked'] else '관측 부족' if rate is None else ''
            _, buff_times = buff_cell(row['exp_buff_intervals'])
            details = row_details(row, buff_times)
            cells = [row['started'].strftime('%m/%d %H:%M') if row['started'] else row['session_id'],
                     row['character_name'] or '—', str(row['level'] or '—'), clock(row['active']),
                     short_exp(row['gained']), warning or f"{rate:,.0f}", f"{ratio(row):.0f}%", meso_cell(row),
                     row['memo']]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(details)
                if column not in editable_columns:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column >= 4: item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setData(Qt.ItemDataRole.UserRole, row['session_id'])
                if column == character_column:
                    detected = row['detected_character']
                    if row['character_name'] != detected:
                        item.setToolTip(f"직접 고친 이름 · 자동 인식: {detected or '없음'}")
                    else:
                        item.setToolTip('두 번 눌러 고치기' if detected else '캐릭터 미확인 · 두 번 눌러 이름 적기')
                    if not row['character_name']: item.setForeground(QColor('#84919f'))
                if column == memo_column:
                    item.setToolTip('두 번 눌러 사냥터 적기' if not row['memo'] else details)
                if column == ratio_column and ratio(row) < 50:
                    item.setForeground(QColor('#e7b86e'))
                if column == rate_column and row['blocked']:
                    item.setForeground(QColor('#e7b86e'))
                if column == rate_column and not warning and rate == best:
                    item.setForeground(QColor('#8fe0a8'))
                table.setItem(index, column, item)
        table.blockSignals(False)
        empty.setVisible(not rows)
        refresh_spots(); refresh_summary()

    table.itemChanged.connect(edited)
    character_picker.currentIndexChanged.connect(render_records)
    period_picker.currentIndexChanged.connect(render_records)
    search_input.textChanged.connect(render_records)
    session_name = controller.session.character_name or getattr(controller, 'current_character', None)
    if session_name in character_names:
        character_picker.setCurrentIndex(character_picker.findData(session_name))
    else:
        render_records()
    return dialog
