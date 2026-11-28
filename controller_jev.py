"""JEV integration, map-name tagging and efficiency-change prompts for SharedController."""
import queue

from PySide6.QtWidgets import QInputDialog

import character_hud
import exp_buffs
import jev_assist
import map_name
import tracker


class JevMixin:
    def update_jev_button(self):
        self.jev_button.setChecked(self.jev_enabled)
        self.jev_button.setText('JEV 켬' if self.jev_enabled else 'JEV 끔')
        scope = '전체 화면 포함' if getattr(self, 'jev_full_screen', True) else '숫자·일부 영역만'
        self.jev_button.setToolTip(
            f'JEV 활성화 ({scope})\n'
            'OCR이 헷갈리거나 상황 판단이 필요할 때만 게임 화면 일부를 JEV에 보내 확인\n'
            '사냥터 이름 · 메소/경험치 판독 · 닉네임 · 버프 · 경험치가 멈춘 이유 · 효율 변화 태그 · 화면 상태\n'
            '우클릭 메뉴에서 API 키 및 전체 화면 전송 허용 설정'
            if self.jev_enabled else 'JEV를 켜면 판독이 헷갈릴 때와 상황 판단에 JEV를 사용 (게임 화면 일부를 전송)')

    def toggle_jev(self, enabled):
        self.jev_enabled = enabled
        self.jev_revision += 1
        self.jev_status_session = None
        self.set_jev_status('')
        self.update_jev_button()
        self.save_config()

    def set_jev_full_screen(self, enabled):
        self.jev_full_screen = bool(enabled)
        self.update_jev_button()
        self.save_config()

    def set_card_line(self, label, text, visible=None):
        """Show a recognition-card line only while it has text; refit the window when it appears or goes."""
        visible = bool(text) if visible is None else visible
        if label.text() != text:
            label.setText(text)
        if label.isHidden() == visible:
            label.setVisible(visible)
            if not self.compact:
                from display_panel import resize
                resize(self)

    def set_jev_status(self, text):
        if self.jev_status.text() != text:
            self.jev_button.setAccessibleDescription(text)
        self.set_card_line(self.jev_status, text)

    def start_map_reading(self, session_id, full):
        """Read the minimap name once per measurement, off the UI thread."""
        if getattr(self, 'map_read_for', None) == session_id or not map_name.available():
            return
        self.map_read_for = session_id
        revision = self.jev_revision if self.jev_enabled else None

        def can_request():
            return (revision is not None and revision == self.jev_revision
                    and session_id == self.session.session_id and not self.stop.is_set())

        def on_request(has_key):
            if not can_request():
                return False
            status = 'JEV 확인 중…' if has_key else 'JEV API 키 필요 · 우클릭 메뉴에서 설정'
            self.map_results.put((session_id, revision, status))
            return True

        def work():
            raw = picked = None
            try:
                title = map_name.title_area(full)
                raw = map_name.read_title(title)
                if can_request():
                    picked = map_name.choose(raw, title, on_request=on_request)
            except Exception as exc:
                tracker.log_error('사냥터 이름 인식', exc)
            self.map_results.put((session_id, revision, (raw, picked)))

        self._start_thread(target=work, name='map-name')

    def apply_map_results(self):
        from record_history import load_memos, save_memo
        current_session = self.session.session_id
        if self.jev_status_session is not None and self.jev_status_session != current_session:
            self.jev_status_session = None
            self.set_jev_status('')
        while True:
            try:
                session_id, revision, result = self.map_results.get_nowait()
            except queue.Empty:
                return
            current_request = self.jev_enabled and revision == self.jev_revision
            if isinstance(result, str):
                if current_request and session_id == current_session:
                    self.jev_status_session = session_id
                    self.set_jev_status(result)
                continue
            raw, picked = result
            if not current_request:
                picked = None
            if (current_request and session_id == self.jev_status_session
                    and self.jev_status.text() == 'JEV 확인 중…'):
                self.set_jev_status(f'JEV로 확인 · {picked}' if picked else 'JEV 결과 없음')
            if not (raw or picked) or load_memos().get(session_id):
                continue  # never overwrite a memo the user already wrote
            try:
                name = save_memo(session_id, picked or map_name.resolve(raw))
                if raw:
                    if picked:
                        map_name.learn_alias(raw, picked)  # same misread next time needs no JEV call
                    map_name.remember_auto(session_id, raw)
                if session_id == current_session:
                    self.status.set(f'사냥터 자동 인식 · {name}')
            except OSError as exc:
                tracker.log_error('사냥터 자동 메모 저장', exc)

    # ---- JEV: ask once per situation; keep the local result whenever JEV is unsure ----

    def jev_nickname(self, frame, level_roi, style, key):
        """Worker thread: a known character name JEV recognised in the nickname line, or None."""
        found, name = self.jev_arbiter.answer(('nick', key))
        if found:
            return name
        crops = character_hud.lines(frame, level_roi, style)
        if crops is not None:
            def question():
                import record_history
                names = [*([self.current_character] if self.current_character else []),
                         *self.nickname_patterns.values(), *record_history.known_characters()]
                return jev_assist.ask_nickname(crops[1], names)
            self.jev_arbiter.submit(('nick', key), question, label='닉네임')
        return None

    def jev_buff_readings(self, frame, stamp, readings):
        """Worker thread: settle 'unknown' buff readings with one JEV look per kind and period."""
        readings = dict(readings)
        bucket = int(stamp // jev_assist.BUFF_SECONDS)
        for kind, value in readings.items():
            if value != 'unknown':
                continue
            question = ('buff', kind, bucket)
            found, verdict = self.jev_arbiter.answer(question)
            if found:
                if verdict:
                    readings[kind] = verdict
                continue
            corner = exp_buffs.buff_corner(frame)

            def ask(kind=kind, corner=corner):
                from PIL import Image
                with Image.open(exp_buffs.template_path(tracker.BASE, kind)) as icon:
                    return jev_assist.ask_buff(icon.convert('RGB'), corner)
            self.jev_arbiter.submit(question, ask, label='버프 확인')
        return readings

    def note_missing_hud(self, generation, full):
        """Worker thread: once the HUD stays missing, ask (at most every 5 minutes) what the window shows."""
        now = self._monotonic()
        if not self.running or self.session.first is not None:
            self.jev_hud_missing = None
            return
        missing = self.jev_hud_missing
        if missing is None or missing[0] != generation:
            self.jev_hud_missing = (generation, now, None)
            return
        if full is None or now - missing[1] < jev_assist.SCREEN_SECONDS:
            return
        question = ('screen', generation, int(now // jev_assist.SCREEN_REPEAT_SECONDS))
        if self.jev_arbiter.submit(question, lambda: jev_assist.ask_screen(full),
                                   full_screen=True, label='화면 확인'):
            self.jev_hud_missing = (generation, missing[1], question)

    def locate_inventory(self, source, stamp, frame):
        """Meso thread: when the meso box stays missing, ask JEV where the inventory is."""
        reader = self.meso_reader
        if reader.located or reader.needs_recheck:
            self.meso_missing = None
            self.meso_inventory_closed = False
            return
        missing = getattr(self, 'meso_missing', None)
        if missing is None or missing[0] != source:
            self.meso_missing = (source, stamp, None, None)
            return
        _, since, asked, asked_at = missing
        if asked is not None:
            found, answer = self.jev_arbiter.answer(asked)
            if not found:
                return
            if answer not in (None, 'closed'):
                reader.hint = jev_assist.quadrant_region(answer)
                self.meso_inventory_closed = False
                return
            self.meso_inventory_closed = answer == 'closed'
            if stamp - asked_at < jev_assist.MESO_CLOSED_SECONDS:
                return
        elif stamp - since < jev_assist.MESO_MISSING_SECONDS:
            return
        question = ('inventory', source, stamp)
        if self.jev_arbiter.submit(question, lambda: jev_assist.ask_inventory(frame),
                                   full_screen=True, label='인벤토리 위치'):
            self.meso_missing = (source, since, question, stamp)

    def change_suggestion(self, change):
        """JEV's tag for an efficiency change, from frames before and after it, or None."""
        if not self.jev_enabled or self.session.started is None:
            return None
        question = ('change', self.session.session_id, change['minute'])
        found, tag = self.jev_arbiter.answer(question)
        if found:
            return tag
        before = self.jev_frames.before(self.session.started + (change['minute'] - 1) * 60)
        after = self.jev_frames.latest(since=self.session.started + change['minute'] * 60)
        if before is not None and after is not None:
            self.jev_arbiter.submit(question, lambda: jev_assist.ask_change(before, after),
                                    full_screen=True, label='효율 변화')
        return None

    def meso_change_suggestion(self, index, delta):
        """JEV's cause hint for a large meso change ('거래·구매·창고' or '사냥 획득'), or None."""
        if not self.jev_enabled or not self.running:
            return None
        question = ('meso_change', self.session.session_id, index)
        found, verdict = self.jev_arbiter.answer(question)
        if found:
            return verdict
        frame = self.jev_frames.latest()
        if frame is not None:
            self.jev_arbiter.submit(question, lambda: jev_assist.ask_meso_change(frame, delta),
                                    full_screen=True, label='큰 메소 변동')
        return None

    def apply_jev_hints(self, now):
        """Show JEV answers for the current measurement and start the questions that are due."""
        session = self.session
        enabled = self.jev_enabled and self.running
        # What the window shows while the level and EXP display cannot be found.
        missing = self.jev_hud_missing
        hint = ''
        if enabled and missing and missing[0] == self.generation and missing[2] and session.first is None:
            hint = self.jev_arbiter.answer(missing[2])[1] or ''
        if hint:
            self.set_jev_status(f'JEV 화면 확인 · {hint}')
        elif self.jev_status.text().startswith('JEV 화면 확인'):
            self.set_jev_status('')
        # Why EXP stopped: asked once per stall, saved with the record.
        activity = self.hunt_activity
        stall = ''
        if (enabled and session.started is not None and activity.generation == self.generation
                and activity.gained > float('-inf') and now - activity.gained >= jev_assist.STALL_SECONDS):
            question = ('stall', session.session_id, activity.gained)
            found, reason = self.jev_arbiter.answer(question)
            if not found:
                frame = self.jev_frames.latest(since=activity.gained + 30)
                if frame is not None:
                    self.jev_arbiter.submit(question, lambda: jev_assist.ask_stall(frame),
                                            full_screen=True, label='멈춘 이유')
            elif reason:
                minutes = int(now - activity.gained) // 60
                stall = f'경험치 멈춤 {minutes}분 · JEV 판단: {reason}'
                suffix = f' · JEV: {reason}'
                if not self.activity_label.text().endswith(suffix):
                    self.activity_label.setText(self.activity_label.text() + suffix)
                if self.jev_stall_saved != question:
                    self.jev_stall_saved = question
                    minute = int(max(0.0, activity.gained - session.started) // 60)
                    try:
                        jev_assist.save_stall(session.session_id, minute, reason)
                    except OSError as exc:
                        tracker.log_error('멈춘 이유 저장', exc)
        self.set_card_line(self.stall_label, stall)
        # Suggested tag for an efficiency change.
        change = getattr(self, 'pending_change', None)
        if change is not None and enabled:
            suggestion = self.change_suggestion(change)
            text = self.change_prompt_text + (f' · JEV 추천: {suggestion}' if suggestion else '')
            if self.change_prompt.text() != text:
                self.change_prompt.setText(text)

    def check_efficiency_change(self):
        """After each new minute, ask once about a confirmed change in efficiency."""
        from qt_controller import short_exp
        count = len(self.session.minute_snapshots)
        key = (self.session.session_id, count)
        if key == getattr(self, '_change_checked', None):
            return
        self._change_checked = key
        pending = None
        if self.running and count:
            import segments
            asked = segments.load_tags().get(self.session.session_id, {})
            for change in segments.find_changes(self.session.minute_snapshots):
                if str(change['minute']) not in asked:
                    pending = change
        self.pending_change = pending
        if pending is None:
            self.change_prompt.setVisible(False)
        else:
            direction = '▲' if pending['after'] > pending['before'] else '▼'
            self.change_prompt_text = (
                f"{direction} {pending['minute']}분째부터 분당 {short_exp(pending['before'])} → {short_exp(pending['after'])}"
                ' · 뭐가 달라졌나요? (눌러서 태그)')
            self.change_prompt.setText(self.change_prompt_text)
            if self.change_prompt.isHidden():
                self.change_prompt.setVisible(True)
                from display_panel import resize
                resize(self)

    def answer_change_prompt(self):
        change = getattr(self, 'pending_change', None)
        if change is None:
            return
        import segments
        suggestion = self.change_suggestion(change)
        text, ok = QInputDialog.getText(
            self.window, '효율 변화',
            f"{change['minute']}분째부터 분당 {change['before']:,.0f} → {change['after']:,.0f}\n"
            '여기서 뭐가 달라졌나요? (예: 버프 끝, 파티 합류, 맵 이동)\n비워 두고 확인하면 다시 묻지 않아요.'
            + ('\nJEV가 화면을 비교해 추천한 태그를 미리 넣어 뒀어요.' if suggestion else ''),
            text=suggestion or '')
        if not ok:
            return
        try:
            segments.save_tag(self.session.session_id, change['minute'], text)
        except OSError as exc:
            tracker.log_error('효율 변화 태그 저장', exc)
            return
        self._change_checked = None
        self.change_prompt.setVisible(False)
        from display_panel import resize
        resize(self)
