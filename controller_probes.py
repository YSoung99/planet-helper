"""Background meso, character HUD, buff and hunting-activity probes for SharedController."""
from datetime import datetime

import character_hud
import exp_buffs
import hunt_activity
import map_name
import meso
import potions
import tracker


def _clock_mmss(seconds):
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


class MesoAndProbesMixin:
    CHARACTER_RECHECK_SECONDS = 8.0
    MESO_IDLE_SCAN_INTERVAL = 4.0

    def meso_character(self):
        if self.running:
            if self.session.character_conflict:
                return None
            return self.session.character_name or self.current_character or ''
        return self.character_identity[1] or self.current_character or ''

    def clear_meso_confirmation(self, source):
        self.meso_source = source
        self.meso_visible = False
        self.meso_confirmation = meso.Confirmation()
        self.meso_rechecking = False

    def meso_scan_interval(self, waiting_for_start):
        if waiting_for_start:
            return 1.0
        if self.meso_reader.located or self.meso_visible or self.meso_rechecking:
            return hunt_activity.SCAN_INTERVAL
        return self.MESO_IDLE_SCAN_INTERVAL

    def seed_meso_from_idle(self, generation, window, reference_stamp):
        """Seed the new session's starting meso from a recent confirmed idle reading."""
        idle = getattr(self, 'idle_meso', None)
        if idle is None or window is None:
            return False
        idle_stamp, idle_window, idle_char, idle_value = idle
        if idle_window != window:
            return False
        if not (0 <= reference_stamp - idle_stamp <= meso.PRE_START_WINDOW):
            return False
        character = self.meso_character()
        if character is None:
            return False
        if character and idle_char and character != idle_char:
            return False
        resolved_char = character or idle_char or ''
        source = (generation, resolved_char, window)
        active_at_seed = 0.0
        if self.session.started is not None and idle_stamp > self.session.started:
            active_at_seed = self.session.active_elapsed(idle_stamp)
        if self.session.meso.seed(idle_value, source, reference_stamp, opening=0, active_seconds=active_at_seed):
            self.clear_meso_confirmation(source)
            self.session.meso_dirty = True
            return True
        return False

    def apply_meso_results(self, now, *, finishing=False):
        session = self.session
        character = self.meso_character()
        pending_start = session.meso_start_frame
        if pending_start is not None:
            stamp, frame, window = pending_start
            if finishing or not self.running or now - stamp > meso.END_WINDOW:
                session.meso_start_frame = None
            elif self.queue_meso_frame(self.generation, stamp, frame, window):
                session.meso_start_frame = None
        if self.meso_source is not None:
            if (self.meso_source[0] != self.generation or character is None
                    or (self.meso_source[1] and character and self.meso_source[1] != character)):
                self.clear_meso_confirmation(None)
            elif not self.meso_source[1] and character:
                self.meso_source = (self.meso_source[0], character, self.meso_source[2])
        if session.meso.source is not None:
            if (session.meso.source[0] != self.generation or character is None
                    or (session.meso.source[1] and character and session.meso.source[1] != character)):
                session.meso.check_source(None)
            elif not session.meso.source[1] and character:
                session.meso.check_source((session.meso.source[0], character, session.meso.source[2]))
        while not self.meso_results.empty():
            generation, window, stamp, observed_character, value = self.meso_results.get_nowait()
            if generation != self.generation or window is None or stamp < now - meso.END_WINDOW:
                continue
            if not self.running:
                idle_source = (generation, window)
                if getattr(self, 'idle_meso_source', None) != idle_source:
                    self.idle_meso_source = idle_source
                    self.idle_meso_confirmation = meso.Confirmation()
                if value is meso.RECHECK:
                    self.idle_meso_confirmation.observe(None, stamp)
                    continue
                self.meso_visible = value is not None
                confirmed = self.idle_meso_confirmation.observe(value, stamp)
                if confirmed is not None:
                    idle_char = character or observed_character or ''
                    self.idle_meso = (stamp, window, idle_char, confirmed)
                continue
            if (character is None or session.meso_start_at is None
                    or stamp < session.meso_start_at):
                continue
            if observed_character and character and observed_character != character:
                continue
            resolved_char = character or observed_character or ''
            source = generation, resolved_char, window
            session.meso.check_source(source)
            if not meso.sources_compatible(self.meso_source, source):
                self.clear_meso_confirmation(source)
            elif self.meso_source is None or (not self.meso_source[1] and resolved_char):
                self.meso_source = source
            if value is meso.RECHECK:
                self.meso_rechecking = True
                self.meso_confirmation.observe(None, stamp)
                continue
            if value is None:
                self.meso_visible = False
            elif not self.meso_visible:
                self.meso_opening += 1
                self.meso_visible = True
            confirmed = self.meso_confirmation.observe(value, stamp)
            if confirmed is None:
                if value is not None and session.meso.end is not None:
                    self.meso_rechecking = True
                continue
            self.meso_rechecking = False
            self.idle_meso = (stamp, window, resolved_char, confirmed)
            now_wall = datetime.now()
            try:
                self.meso_log.record(tracker.BASE, resolved_char, confirmed, now_wall)
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
            active_sec = session.active_elapsed(stamp) if session.started is not None else 0.0
            prev_large = len(session.meso.large_changes)
            session.meso_dirty |= session.meso.sample(
                confirmed, source, stamp, self.meso_opening, session.meso_start_at,
                self.meso_trade_threshold, first_seen=self.meso_confirmation.first_seen,
                active_seconds=active_sec, when=now_wall)
            if len(session.meso.large_changes) > prev_large:
                self._capture_meso_change_frame(session.session_id, len(session.meso.large_changes) - 1, generation, stamp)
        if (not finishing and self.running and session.meso_dirty and session.rows and not session.ledger_dirty
                and now - self.meso_last_flush >= 10):
            self.meso_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
        self.update_meso_display(now)

    def update_meso_display(self, now):
        session = self.session
        text = self.meso_text()
        if text != self.meso_label.text():
            self.meso_label.setText(text)
        ready = self.meso_status(now)
        if self.meso_status_label.text() != ready:
            self.meso_status_label.setText(ready)
        delta = session.meso.hunted()
        if (self.running and not session.meso.source_changed
                and meso.sources_compatible(session.meso.source, self.meso_source)):
            delta = session.meso.observed_delta()
        amount = '—' if delta is None else f'{delta:+,}'
        if self.meso_delta_label.text() != amount:
            self.meso_delta_label.setText(amount)
        rate_text, rate_tip = self.meso_rate_summary(now)
        if hasattr(self, 'meso_rate_label'):
            if self.meso_rate_label.text() != rate_text:
                self.meso_rate_label.setText(rate_text)
            if self.meso_rate_label.toolTip() != rate_tip:
                self.meso_rate_label.setToolTip(rate_tip)
        if hasattr(self, 'compact_meso'):
            compact_text, compact_tip = self.compact_meso_text(now)
            if self.compact_meso.text() != compact_text:
                self.compact_meso.setText(compact_text)
            if self.compact_meso.toolTip() != compact_tip:
                self.compact_meso.setToolTip(compact_tip)
        self.update_meso_change_prompt()

    def update_meso_change_prompt(self):
        prompt = getattr(self, 'meso_change_prompt', None)
        if prompt is None:
            return
        session = self.session
        if getattr(self, 'meso_prompt_session', None) != session.session_id:
            self.meso_prompt_session = session.session_id
            self.meso_prompt_reviewed = set()
        balance = session.meso
        unreviewed = [
            idx for idx in range(len(balance.large_changes))
            if idx not in self.meso_prompt_reviewed and idx not in balance.marked
        ] if self.running and not balance.source_changed else []
        if not unreviewed:
            if not prompt.isHidden():
                prompt.setVisible(False)
                from display_panel import resize
                resize(self)
            return
        idx = unreviewed[-1]
        hint = self.meso_change_suggestion(idx, balance.large_changes[idx]) if hasattr(self, 'meso_change_suggestion') else None
        # Confident hunting profit / NPC loot sale: quietly keep as hunting income without nagging.
        if hint == '사냥 획득':
            self.meso_prompt_reviewed.add(idx)
            unreviewed = [i for i in unreviewed if i != idx]
            if not unreviewed:
                if not prompt.isHidden():
                    prompt.setVisible(False)
                    from display_panel import resize
                    resize(self)
                return
            idx = unreviewed[-1]
            hint = self.meso_change_suggestion(idx, balance.large_changes[idx]) if hasattr(self, 'meso_change_suggestion') else None
        text = f'큰 메소 변동 {balance.describe_change(idx)} · 눌러서 분류'
        if len(unreviewed) > 1:
            text += f' (+{len(unreviewed) - 1}건)'
        if hint:
            text += f' · {hint}'
        if prompt.text() != text:
            prompt.setText(text)
        if prompt.isHidden():
            prompt.setVisible(True)
            from display_panel import resize
            resize(self)

    def _current_meso_rate(self, now):
        session = self.session
        balance = session.meso
        if self.running:
            if balance.source_changed or not meso.sources_compatible(balance.source, self.meso_source):
                return None
            fallback = session.active_elapsed(now) if session.started is not None else None
            return balance.rate(fallback_active=fallback)
        if balance.hunted() is None:
            return None
        fallback = session.active_elapsed(now) if session.started is not None else None
        return balance.rate(fallback_active=fallback)

    def meso_rate_summary(self, now):
        balance = self.session.meso
        if balance.source_changed:
            return '시간당 메소 · 캐릭터 또는 게임 창 변경으로 집계 중단', ''
        info = self._current_meso_rate(now)
        if info is None or info['delta'] is None:
            if balance.start is not None:
                return '시간당 메소 · 인벤토리를 다시 열거나 잔액이 바뀌면 표시', ''
            return '시간당 메소 · 측정 중 인벤토리를 열면 집계', ''
        seconds = info['seconds']
        if not info['ready']:
            span = _clock_mmss(seconds) if seconds else '0:00'
            return (f'메소 측정 구간 {span} · 1분 이상 측정 시 시급 표시',
                    f'현재 순증감 {info["delta"]:+,} 메소 ({span})')
        parts = [
            f'시간당 {meso.format_short(info["per_hour"])}',
            f'분당 {info["per_minute"]:+,.0f}',
        ]
        if info['recent_per_hour'] is not None:
            trend = ''
            if info['trend_ratio'] is not None and abs(info['trend_ratio']) >= 0.05:
                arrow = '▲' if info['trend_ratio'] > 0 else '▼'
                trend = f' {arrow}{abs(info["trend_ratio"]) * 100:.0f}%'
            parts.append(f'최근 {meso.format_short(info["recent_per_hour"])}/시{trend}')
        parts.append(f'구간 {_clock_mmss(seconds)}')
        tip = (f'메소 순증감 {info["delta"]:+,} · 측정 사냥 시간 {_clock_mmss(seconds)}\n'
               f'시간당 {info["per_hour"]:+,.0f} · 분당 {info["per_minute"]:+,.0f}')
        if info['recent_per_hour'] is not None:
            tip += (f'\n최근 {_clock_mmss(info["recent_seconds"])} 구간: '
                    f'{info["recent_delta"]:+,} (시간당 {info["recent_per_hour"]:+,.0f})')
        return ' · '.join(parts), tip

    def compact_meso_text(self, now):
        balance = self.session.meso
        info = self._current_meso_rate(now)
        if self.running and self.meso_rechecking:
            current = balance.end if balance.end is not None else self.meso_confirmation.current
            prefix = f'메소 {current:,}' if current is not None else '메소'
            return f'{prefix} · 재확인 중', '원본과 흑백 판독이 달라 다시 확인 중입니다.'
        if info is not None and info['delta'] is not None:
            delta_str = meso.format_short(info['delta'])
            if info['ready']:
                trend = ''
                if info['trend_ratio'] is not None and abs(info['trend_ratio']) >= 0.05:
                    arrow = '▲' if info['trend_ratio'] > 0 else '▼'
                    trend = f' ({arrow}{abs(info["trend_ratio"]) * 100:.0f}%)'
                text = f'메소 {delta_str} · 시급 {meso.format_short(info["per_hour"])}{trend}'
            else:
                text = f'메소 {delta_str} · 구간 {_clock_mmss(info["seconds"] or 0)}'
            _, tip = self.meso_rate_summary(now)
            return text, tip
        if self.running:
            if balance.source_changed:
                return '메소 확인 불가 · 창/캐릭터 변경', '캐릭터나 게임 창이 바뀌어 이번 메소 집계를 중단했습니다.'
            if balance.start is not None:
                return f'메소 {balance.start:,} · 변동/재확인 대기', '인벤토리를 다시 열거나 잔액이 바뀌면 순증감과 시급을 표시합니다.'
            return '메소 대기 · 인벤토리 열면 측정', '측정 중 언제든 인벤토리를 열면 메소 측정이 시작됩니다.'
        idle = getattr(self, 'idle_meso', None)
        if idle is not None:
            return f'메소 {idle[3]:,} · 대기 중 확인됨', '자동 측정 시작 시 이 잔액을 시작 기준에 바로 반영합니다.'
        return '메소 대기 · 인벤토리 열면 측정', '대기 중이나 측정 중 인벤토리를 열면 잔액을 읽습니다.'

    def meso_status(self, now):
        balance = self.session.meso
        if balance.source_changed:
            return '시작·종료 잔액 확인 불가 · 캐릭터 또는 게임 창 변경'
        if balance.start is not None:
            start = '시작 잔액 확인 (대기 연동)' if balance.seeded else '시작 잔액 확인'
        else:
            start = '시작 잔액 대기'
        end = balance.complete
        if self.running:
            end = not self.meso_rechecking and balance.end_ready(self.meso_source, now)
        return start + (' · 종료 잔액 확인' if end else ' · 종료 잔액 대기')

    def meso_text(self):
        current = self.meso_confirmation.current
        if self.running and self.meso_rechecking:
            if self.session.meso.end is not None:
                current = self.session.meso.end
            return ('메소' if current is None else f'메소 {current:,}') + ' · 잔액 재확인 중'
        if current is None:
            if self.running and self.session.meso.end is not None:
                current = self.session.meso.end
            elif not self.running and getattr(self, 'idle_meso', None) is not None:
                idle_val = self.idle_meso[3]
                today = self.meso_log.change(self.meso_character())
                return f'메소 {idle_val:,} · 대기 중 확인' + (f' · 오늘 {today:+,}' if today is not None else '')
            else:
                if self.running and getattr(self, 'meso_inventory_closed', False) and self.jev_enabled:
                    return '메소 · 인벤토리가 닫혀 있어요 · 열면 기록'
                return '메소 · 측정 중 인벤토리를 열면 기록'
        text = f'메소 {current:,}'
        balance = self.session.meso
        if self.running:
            if balance.source_changed:
                return text + ' · 캐릭터 또는 게임 창 변경 · 이번 잔액 차이 확인 안 됨'
            if balance.readings < 2 or not meso.sources_compatible(balance.source, self.meso_source):
                if balance.start is None:
                    return text + ' · 인벤토리 확인 중'
                return text + ' · 인벤토리를 다시 열거나 잔액이 바뀌면 집계'
            if balance.large_changes:
                text += f' · 큰 변동 {len(balance.large_changes)}건'
            excluded = balance.excluded_trade()
            if excluded:
                text += f' · 거래·구매 {excluded:+,} 제외'
            return text
        today = self.meso_log.change(self.meso_character())
        return text + (f' · 오늘 {today:+,}' if today is not None else '')

    def meso_diagnostic_text(self, now):
        """Detailed diagnostic text for the 인식 정보 dialog."""
        mode = '수동 영역 지정됨' if getattr(self, 'meso_region', None) is not None else '자동 탐색'
        if self.meso_region is None and self.meso_reader.hint is not None:
            mode += ' (JEV 위치 힌트 사용)'
        lines = [f'탐색 방식 · {mode}']
        diag_entry = getattr(self, 'meso_diag', None)
        if diag_entry is not None:
            stamp, diag = diag_entry
            age = max(0, int(now - stamp))
            reason = diag.get('reason') or '확인 중'
            box = diag.get('box')
            box_text = f' · 위치 ({box[0]}, {box[1]}, {box[2]}×{box[3]})' if box else ''
            lines.append(f'최근 판독 ({age}초 전) · {reason}{box_text}')
        else:
            lines.append('최근 판독 · 아직 인벤토리 스캔 전')
        balance = self.session.meso
        if balance.start is not None:
            span = balance.measured_seconds()
            span_text = f' · 측정 구간 {_clock_mmss(span)}' if span else ''
            delta = balance.observed_delta()
            delta_text = f' · 순증감 {delta:+,}' if delta is not None else ''
            lines.append(f'세션 잔액 · 시작 {balance.start:,} → 현재 {balance.end:,}{delta_text}{span_text}')
            rate_text, _ = self.meso_rate_summary(now)
            lines.append(rate_text)
        elif getattr(self, 'idle_meso', None) is not None:
            idle_stamp, _, _, idle_val = self.idle_meso
            lines.append(f'대기 확인 잔액 · {idle_val:,} ({max(0, int(now - idle_stamp))}초 전)')
        return '\n'.join(lines)

    def start_meso_capture(self, generation, stamp, details):
        full = details.get('full_game_image')
        window = details.get('window_id')
        self.seed_meso_from_idle(generation, window, stamp)
        if full is not None and window is not None and map_name.available():
            if not self.queue_meso_frame(generation, stamp, full, window):
                self.session.meso_start_frame = stamp, full, window
        self.session.meso_start_at = stamp

    def queue_meso_frame(self, generation, stamp, frame, window):
        if not self.meso_capture_lock.acquire(blocking=False):
            return False
        try:
            self.submit_probe(self.read_meso_frame, generation, stamp, frame, window, self.meso_character())
        except RuntimeError as exc:
            self.meso_capture_lock.release()
            tracker.log_error('메소 판독 시작', exc)
            return False
        return True

    def _capture_meso_change_frame(self, session_id, index, generation, stamp):
        """Record change timestamp (for local potion cross-check) and pin exact frame for JEV."""
        import jev_assist
        stamps = getattr(self, 'meso_change_stamps', None)
        if stamps is None:
            stamps = {}
            self.meso_change_stamps = stamps
        stamps[(session_id, index)] = stamp
        while len(stamps) > 32:
            stamps.pop(next(iter(stamps)))
        if not getattr(self, 'jev_enabled', False):
            return
        last = getattr(self, '_last_meso_frame', None)
        frame = last[2] if (last is not None and last[0] == generation) else self.jev_frames.latest()
        if frame is None:
            return
        self.jev_frames.remember(stamp, frame, force=True)
        store = getattr(self, 'meso_change_frames', None)
        if store is None:
            store = {}
            self.meso_change_frames = store
        store[(session_id, index)] = jev_assist.FrameRing._encode(frame)
        while len(store) > 16:
            store.pop(next(iter(store)))

    def read_meso_frame(self, generation, stamp, frame, window, character):
        try:
            if generation == self.generation:
                self._last_meso_frame = (generation, stamp, frame)
                source = generation, window
                manual_roi = getattr(self, 'meso_region', None)
                if self.meso_reader_source != source:
                    self.meso_reader = meso.Reader(self.jev_arbiter, manual_roi=manual_roi)
                    self.meso_reader_source = source
                elif self.meso_reader.manual_roi != manual_roi:
                    self.meso_reader.set_manual_roi(manual_roi)
                value = self.meso_reader.read(frame, stamp)
                if self.meso_reader.needs_recheck:
                    value = meso.RECHECK
                if self.meso_reader.last_diag is not None:
                    self.meso_diag = (stamp, dict(self.meso_reader.last_diag))
                if self.meso_reader.last_crop is not None:
                    self.meso_preview_frame = (self.meso_reader.last_crop.copy(), stamp)
                if self.running and manual_roi is None:
                    self.locate_inventory(source, stamp, frame)
                self.meso_results.put((generation, window, stamp, character, value))
        except Exception as exc:
            tracker.log_error('메소 판독', exc)
        finally:
            self.meso_capture_lock.release()

    def read_hunt_activity(self, generation, stamp, frame, window, level_roi, style, recent=False):
        try:
            try:
                if level_roi is not None and window is not None and map_name.available():
                    job, nickname, key = character_hud.inspect(frame, level_roi, style, self.nickname_patterns)
                    if nickname is None and key is not None:
                        nickname = self.jev_nickname(frame, level_roi, style, key)
                    self.character_results.put((generation, window, stamp, job, nickname, key))
            except Exception as exc:
                tracker.log_error('캐릭터 HUD 판독', exc)
            if recent:
                return
            if self.jev_enabled:
                self.jev_frames.remember(stamp, frame)
            retry = getattr(self, 'map_retry_at', None)
            if (retry is not None and self.running and self.session.started is not None
                    and retry[0] == self.session.session_id and stamp >= retry[1]):
                self.map_retry_at = None
                self.start_map_reading(self.session.session_id, frame, retry=True)
            try:
                if window is not None and self.buff_has_templates:
                    revision = self.buff_revision
                    readings = self.jev_buff_readings(frame, stamp, self.buff_detector.read(frame, tracker.BASE))
                    self.buff_results.put((generation, window, revision, stamp, readings))
            except Exception as exc:
                tracker.log_error('경험치 버프 판독', exc)
            try:
                if map_name.available():
                    observation = hunt_activity.read(frame, self.hunt_activity_cache)
                    self.hunt_activity_results.put((generation, stamp, observation))
            except Exception as exc:
                tracker.log_error('사냥 신호 판독', exc)
            try:
                slots = list(getattr(self, 'potion_slots', ()))
                reader = self.potion_reader
                if slots and window is not None and map_name.available():
                    readings = reader.read_slots(frame, slots)
                    self.potion_results.put((generation, window, stamp, readings))
            except Exception as exc:
                tracker.log_error('포션 소모 판독', exc)
        finally:
            self.hunt_activity_lock.release()

    def character_read_needed(self, generation, window, now):
        return (generation, window) != self.character_source or now >= self.character_recheck_at

    def set_character_source(self, source):
        if source != self.character_source:
            self.character_source = source
            self.character_candidate = (None, None)
            self.character_identity = (None, None)
            self.character_recheck_at = 0.0
            self.nickname_sample = None
            self.update_capture_label()

    def update_capture_label(self):
        if not self.auto_hud:
            self.source_label.setText('수동 영역')
            return
        job, nickname = self.character_identity
        fields = [f'Lv. {self.session.level}'] if self.running else []
        fields.append(f'접속 닉네임 {nickname}' if nickname else '접속 닉네임 확인 대기')
        if job:
            fields.append(job)
        self.source_label.setText(' · '.join(fields))

    def set_detected_character(self, identity):
        self.character_identity = identity
        self.update_capture_label()
        nickname = identity[1]
        session = self.session
        if self.running and nickname and not session.character_conflict and nickname != session.character_name:
            session.character_conflict = session.character_name is not None
            session.character_name = None if session.character_conflict else nickname
            if session.rows:
                try:
                    self.save_summary()
                except OSError as exc:
                    tracker.log_error('사냥 기록 캐릭터 저장', exc)

    def apply_character_results(self, now):
        while not self.character_results.empty():
            generation, window, stamp, job, nickname, key = self.character_results.get_nowait()
            source = generation, window
            if source != self.character_source or generation != self.generation or now - stamp > 15:
                continue
            previous = self.nickname_sample
            if previous and stamp <= previous[1]:
                continue
            self.nickname_sample = (source, stamp, key) if key else None
            old_job, old_name = self.character_candidate
            registered = self.nickname_patterns.get(key)
            confirmed_name = registered or (nickname if nickname and nickname == old_name
                                            and previous and previous[2] == key else None)
            self.character_candidate = (job, nickname)
            self.set_detected_character((job if job == old_job else None, confirmed_name))
            self.character_recheck_at = now + (self.CHARACTER_RECHECK_SECONDS if confirmed_name else 2.0)
        if self.nickname_sample and now - self.nickname_sample[1] > 15:
            self.set_character_source(None)

    def apply_buff_results(self, now):
        session = self.session
        while not self.buff_results.empty():
            generation, window, revision, stamp, readings = self.buff_results.get_nowait()
            if (not self.running or (generation, window) != self.buff_window
                    or generation != self.generation or revision != self.buff_revision):
                continue
            start = self.buff_confirmation.observe((generation, window), stamp, readings)
            if session.started is not None and start is not None:
                session.exp_buffs_dirty |= exp_buffs.record(
                    session.exp_buff_intervals, self.buff_confirmation.current,
                    start - session.started, stamp - session.started)
        if (self.running and session.exp_buffs_dirty and session.rows and not session.ledger_dirty
                and now - self.buff_last_flush >= 10):
            self.buff_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('경험치 버프 구간 저장', exc)
        if not self.buff_has_templates:
            text = '경험치 버프 · 아이콘 미등록'
        elif self.running:
            text = self.buff_confirmation.text(self.generation, now)
        else:
            text = '경험치 버프 · 측정 대기'
        self.set_card_line(self.buff_label, text, visible=self.buff_has_templates)

    def apply_potion_results(self, now):
        session = self.session
        slots = {slot['name']: slot.get('unit_cost', 0) for slot in getattr(self, 'potion_slots', ())}
        while not self.potion_results.empty():
            generation, window, stamp, readings = self.potion_results.get_nowait()
            if not self.running or generation != self.generation or window is None:
                continue
            source = (generation, window)
            if self.potion_source != source:
                self.potion_source = source
                self.potion_confirmations = {}
            for name, raw_val in readings:
                if name not in slots:
                    continue
                conf = self.potion_confirmations.get(name)
                if conf is None:
                    conf = potions.SlotConfirmation()
                    self.potion_confirmations[name] = conf
                confirmed = conf.observe(raw_val, stamp)
                if confirmed is not None:
                    session.potions_dirty |= session.potions.observe(name, confirmed, slots[name], stamp=stamp)
        if (self.running and session.potions_dirty and session.rows and not session.ledger_dirty
                and now - self.potion_last_flush >= 10):
            self.potion_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('포션 소모 기록 저장', exc)
        self.update_potion_display(now)

    def update_potion_display(self, now):
        label = getattr(self, 'potion_label', None)
        if label is None:
            return
        has_slots = bool(getattr(self, 'potion_slots', ())) or bool(self.session.potions.items)
        if not has_slots:
            self.set_card_line(label, '', visible=False)
            return
        active = self.session.active_elapsed(now) if self.session.started is not None else None
        text = self.session.potions.summary_text(active_seconds=active)
        if not text:
            text = f'포션 · 슬롯 {len(self.potion_slots)}개 인식 대기' if self.running else f'포션 · 슬롯 {len(self.potion_slots)}개 등록됨'
        self.set_card_line(label, text, visible=True)
