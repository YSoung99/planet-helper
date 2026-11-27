"""Background meso, character HUD, buff and hunting-activity probes for SharedController."""
from datetime import datetime

import character_hud
import exp_buffs
import hunt_activity
import map_name
import meso
import tracker


class MesoAndProbesMixin:
    CHARACTER_RECHECK_SECONDS = 8.0
    MESO_IDLE_SCAN_INTERVAL = 4.0

    def meso_character(self):
        if self.session.character_conflict:
            return None
        return self.session.character_name or self.current_character or ''

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
        if self.meso_source is not None and self.meso_source[:2] != (self.generation, character):
            self.clear_meso_confirmation(None)
        if session.meso.source is not None and session.meso.source[:2] != (self.generation, character):
            session.meso.check_source(None)
        while not self.meso_results.empty():
            generation, window, stamp, observed_character, value = self.meso_results.get_nowait()
            if (not self.running or generation != self.generation or character is None
                    or observed_character != character or window is None or session.meso_start_at is None
                    or stamp < session.meso_start_at or stamp < now - meso.END_WINDOW):
                continue
            source = generation, character, window
            session.meso.check_source(source)
            if source != self.meso_source:
                self.clear_meso_confirmation(source)
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
            try:
                self.meso_log.record(tracker.BASE, character, confirmed, datetime.now())
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
            session.meso_dirty |= session.meso.sample(
                confirmed, source, stamp, self.meso_opening, session.meso_start_at,
                self.meso_trade_threshold, first_seen=self.meso_confirmation.first_seen)
        if (not finishing and self.running and session.meso_dirty and session.rows and not session.ledger_dirty
                and now - self.meso_last_flush >= 10):
            self.meso_last_flush = now
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('메소 기록 저장', exc)
        text = self.meso_text()
        if text != self.meso_label.text():
            self.meso_label.setText(text)
        ready = self.meso_status(now)
        if self.meso_status_label.text() != ready:
            self.meso_status_label.setText(ready)
        delta = session.meso.hunted()
        if (self.running and not session.meso.source_changed
                and session.meso.source == self.meso_source):
            delta = session.meso.observed_delta()
        amount = '—' if delta is None else f'{delta:+,}'
        if self.meso_delta_label.text() != amount:
            self.meso_delta_label.setText(amount)

    def meso_status(self, now):
        balance = self.session.meso
        if balance.source_changed:
            return '시작·종료 잔액 확인 불가 · 캐릭터 또는 게임 창 변경'
        start = '시작 잔액 확인' if balance.start is not None else '시작 잔액 대기'
        if (self.running and balance.start is None and self.session.meso_start_at is not None
                and now - self.session.meso_start_at > meso.START_WINDOW):
            start = '시작 잔액 확인 시간 지남'
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
            if self.running and getattr(self, 'meso_inventory_closed', False) and self.jev_enabled:
                return '메소 · JEV: 인벤토리가 닫혀 있어요 · 열면 기록'
            return '메소 · 측정 중 인벤토리를 열면 기록'
        text = f'메소 {current:,}'
        balance = self.session.meso
        if self.running:
            if balance.source_changed:
                return text + ' · 캐릭터 또는 게임 창 변경 · 이번 잔액 차이 확인 안 됨'
            if balance.readings < 2 or balance.source != self.meso_source:
                if balance.start is None:
                    return text + ' · 시작 후 30초 안의 잔액 필요'
                return text + ' · 종료 직전에 인벤토리를 다시 열어 주세요'
            if balance.large_changes:
                text += f' · 큰 변동 {len(balance.large_changes)}건'
            excluded = balance.excluded_trade()
            if excluded:
                text += f' · 거래·구매 {excluded:+,} 제외'
            return text
        today = self.meso_log.change(self.meso_character())
        return text + (f' · 오늘 {today:+,}' if today is not None else '')

    def start_meso_capture(self, generation, stamp, details):
        full = details.get('full_game_image')
        window = details.get('window_id')
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

    def read_meso_frame(self, generation, stamp, frame, window, character):
        try:
            if generation == self.generation:
                source = generation, window
                if self.meso_reader_source != source:
                    self.meso_reader = meso.Reader(self.jev_arbiter)
                    self.meso_reader_source = source
                value = self.meso_reader.read(frame, stamp)
                if self.meso_reader.needs_recheck:
                    value = meso.RECHECK
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
            self.jev_frames.remember(stamp, frame)
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
