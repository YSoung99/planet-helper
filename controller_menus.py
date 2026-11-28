"""Context menu, settings dialogs, backup and cloud sync actions for SharedController."""
from datetime import datetime
from pathlib import Path
import threading
import time

from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout,
)

import character_hud
import exp_buffs
import meso
import tracker


class MenuAndSettingsMixin:
    def opacity_settings(self):
        value, ok = QInputDialog.getInt(
            self.window, '배경 투명도', '배경 불투명도 (%)',
            round(self.window_opacity * 100), 20, 100)
        if ok:
            self.window_opacity = value / 100
            self.window.update()
            self.save_config()

    def repeat_settings(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle('연타 설정과 권한')
        dialog.setStyleSheet('QDialog { background: #151b24; } QComboBox { background: #25303d; padding: 4px; }')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('키를 누르는 동안 게임 창에서만 반복해요.'))
        layout.addWidget(QLabel('설정을 저장하면 연타가 꺼져요.'))
        grid = QGridLayout()
        settings = self.key_repeat.settings
        pickers = {}
        for row, (key, label, low, high) in enumerate((
                ('firstDelayMs', '첫 대기', 100, 2000),
                ('minIntervalMs', '반복 최소 간격', 50, 1000),
                ('maxIntervalMs', '반복 최대 간격', 50, 1000))):
            picker = QSpinBox()
            picker.setRange(low, high)
            picker.setSuffix(' ms')
            picker.setValue(settings[key])
            picker.setAccessibleName(label)
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(picker, row, 1)
            pickers[key] = picker
        trigger = QComboBox()
        trigger.addItem('A 키', 0)
        trigger.addItem('Z 키 → A 입력', 6)
        trigger.setCurrentIndex(1 if settings['attackTriggerKeyCode'] == 6 else 0)
        grid.addWidget(QLabel('공격 키'), 3, 0)
        grid.addWidget(trigger, 3, 1)
        layout.addLayout(grid)
        cleanup = self.add_repeat_permission_widgets(layout)
        failure = QLabel(self.key_repeat.settings_error)
        failure.setWordWrap(True)
        layout.addWidget(failure)
        actions = QHBoxLayout()
        save = QPushButton('저장')
        cancel = QPushButton('닫기')

        def save_settings():
            selected = {key: picker.value() for key, picker in pickers.items()}
            selected['attackTriggerKeyCode'] = trigger.currentData()
            try:
                self.key_repeat.configure(selected)
            except (OSError, ValueError, RuntimeError) as exc:
                failure.setText(str(exc))
                return
            dialog.accept()

        save.clicked.connect(save_settings)
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(save)
        actions.addWidget(cancel)
        layout.addLayout(actions)
        try:
            dialog.exec()
        finally:
            cleanup()

    def _active_session_exclusions(self):
        return {self.session.session_id} if self.running and self.session.session_id else None

    def export_records(self):
        import records_backup
        suggested = str(Path.home() / f"planet-helper-records-{datetime.now():%Y%m%d}.zip")
        path, _ = QFileDialog.getSaveFileName(self.window, '사냥 기록 내보내기', suggested, '백업 (*.zip)')
        if not path:
            return
        if not path.endswith('.zip'):
            path += '.zip'
        try:
            count = records_backup.export_zip(path, exclude_sessions=self._active_session_exclusions())
        except OSError as exc:
            tracker.log_error('기록 내보내기', exc)
            QMessageBox.warning(self.window, '사냥 기록 내보내기', f'내보내지 못했어요.\n{exc}')
            return
        QMessageBox.information(
            self.window, '사냥 기록 내보내기',
            f'기록 {count}개를 저장했어요.\n{path}\n\n'
            '다른 컴퓨터의 플래닛 도우미에서 "백업 파일에서 가져오기"로 합칠 수 있어요.')

    def import_records(self):
        import records_backup
        import zipfile
        path, _ = QFileDialog.getOpenFileName(self.window, '사냥 기록 가져오기', str(Path.home()), '백업 (*.zip)')
        if not path:
            return
        try:
            added, skipped, names = records_backup.import_zip(path)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            tracker.log_error('기록 가져오기', exc)
            QMessageBox.warning(self.window, '사냥 기록 가져오기', f'가져오지 못했어요.\n{exc}')
            return
        QMessageBox.information(
            self.window, '사냥 기록 가져오기',
            f'새 기록 {added}개를 추가했어요.\n'
            f'이미 있는 기록 {skipped}개는 그대로 두었어요.\n'
            f'사냥터·캐릭터 이름 {names}개를 합쳤어요.')

    def run_auto_backup(self):
        """Daily copy of the records, in the background so startup stays quick."""
        excluded = self._active_session_exclusions()
        def work():
            try:
                import records_backup
                records_backup.auto_backup(exclude_sessions=excluded)
            except Exception as exc:
                tracker.log_error('자동 백업', exc)
            if self.cloud_remote:
                self.cloud_work(push_only=False, manual=False)
        threading.Thread(target=work, name='records-backup', daemon=True).start()

    def cloud_settings(self):
        import cloud_sync
        if cloud_sync.rclone() is None:
            QMessageBox.information(
                self.window, '구글 드라이브 동기화',
                'rclone이 필요해요.\n\n리눅스: 패키지 관리자에서 rclone 설치\n맥: brew install rclone\n\n'
                '설치한 뒤 다시 열어 주세요.')
            return
        remotes = cloud_sync.remotes()
        if not remotes:
            QMessageBox.information(
                self.window, '구글 드라이브 동기화',
                '먼저 rclone에 구글 드라이브를 한 번 연결해야 해요. 터미널에서 실행하세요:\n\n'
                '    rclone config create gdrive drive scope=drive.file\n\n'
                '브라우저가 열리면 구글 계정으로 로그인하고 허용을 누르면 돼요.\n'
                '(drive.file 권한: 이 앱이 올린 파일만 볼 수 있어요.)\n\n'
                '끝나면 이 메뉴를 다시 열어 주세요.')
            return
        off = '동기화 끄기'
        current = (self.cloud_remote or '').split(':', 1)[0] + ':' if self.cloud_remote else None
        options = ([current] if current in remotes else []) + [r for r in remotes if r != current] + [off]
        choice, ok = QInputDialog.getItem(
            self.window, '구글 드라이브 동기화',
            '사용할 rclone 연결 (그 안의 planet-helper 폴더에 컴퓨터별 백업을 올려요)', options, 0, False)
        if not ok:
            return
        if choice == off:
            self.cloud_remote = None
            self.save_config()
            self.status.set('구글 드라이브 동기화 꺼짐')
            return
        self.cloud_remote = choice + 'planet-helper'
        self.save_config()
        self.start_cloud_sync(manual=True)

    def start_cloud_sync(self, manual=False, push_only=False):
        if not self.cloud_remote:
            return
        if manual:
            self.status.set('구글 드라이브 동기화 중…')
        threading.Thread(target=self.cloud_work, args=(push_only, manual),
                         name='cloud-sync', daemon=True).start()

    def cloud_work(self, push_only, manual):
        """Runs off the UI thread; reports through a queued signal."""
        import cloud_sync
        remote = self.cloud_remote
        if not remote:
            return
        if not self.cloud_lock.acquire(blocking=False):
            if manual:
                self.cloud_signals.done.emit('이미 동기화 중이에요')
            return
        try:
            excluded = self._active_session_exclusions()
            if push_only:
                cloud_sync.push(remote, exclude_sessions=excluded)
                text = '구글 드라이브에 기록 올림'
            else:
                text = '구글 드라이브 ' + cloud_sync.describe(cloud_sync.sync(remote, exclude_sessions=excluded))
        except Exception as exc:
            tracker.log_error('구글 드라이브 동기화', exc)
            try:
                cloud_sync.save_state(dict(time=time.time(), ok=False, error=str(exc)[:120]))
            except OSError:
                pass
            text = '구글 드라이브 동기화 실패 · last-error.log 확인'
        finally:
            self.cloud_lock.release()
        if manual or not push_only:
            self.cloud_signals.done.emit(text)

    def cloud_finished(self, text):
        self.status.set(text)

    def context_menu(self, point):
        menu = self.build_context_menu()
        try:
            menu.exec(self.window.mapToGlobal(point))
        finally:
            menu.deleteLater()

    def build_context_menu(self):
        menu = QMenu(self.window)
        menu.addAction('사냥 기록 비교', self.show_history)
        menu.addSeparator()

        def add_toggle(parent, title, checked, callback):
            action = parent.addAction(title)
            action.setCheckable(True)
            action.setChecked(checked)
            action.toggled.connect(callback)

        measurement = menu.addMenu('측정·화면')
        add_toggle(measurement, '항상 맨 위', self.always_on_top, self.toggle_pin)
        measurement.addAction('배경 투명도', self.opacity_settings)
        measurement.addSeparator()
        add_toggle(measurement, '경험치 증가 시 자동 측정', self.auto_measure, self.set_auto_measure)
        add_toggle(measurement, '대기 중 최근 경험치 모으기', self.recent_recording, self.set_recent_recording)
        add_toggle(measurement, '플래닛 버닝 적용', self.burning, self.set_burning)
        measurement.addAction('연타 설정', self.repeat_settings)

        recognition = menu.addMenu('게임 인식')
        recognition.addAction(f"현재 캐릭터: {self.current_character or '지정 안 함'}…", self.choose_character)
        recognition.addAction('현재 닉네임 등록·수정…', self.register_nickname)
        recognition.addSeparator()
        recognition.addAction('경험치 영역 지정…', self.select)
        recognition.addAction('JEV API 키 설정', self.api_key_settings)
        add_toggle(recognition, 'JEV 전체 화면 전송 허용 (멈춤·효율·인벤토리·화면)',
                   self.jev_full_screen, self.set_jev_full_screen)

        gains = menu.addMenu('메소·버프')
        gains.addAction('메소 영역 수동 지정…', self.select_meso_region)
        if getattr(self, 'meso_region', None) is not None:
            gains.addAction('메소 자동 탐색으로 되돌리기', self.clear_meso_region)
        gains.addSeparator()
        gains.addAction('포션 슬롯 추가 (영역 지정)…', self.select_potion_slot)
        if getattr(self, 'potion_slots', None):
            gains.addAction(f'등록된 포션 관리 ({len(self.potion_slots)}개)…', self.manage_potion_slots)
        gains.addSeparator()
        gains.addAction('메소 큰 변동 기준 금액', self.meso_threshold_settings)
        gains.addAction('이번 측정의 큰 메소 변동 분류…', self.classify_meso_change)
        gains.addSeparator()
        for kind, label in exp_buffs.LABELS.items():
            gains.addAction(f'{label} 아이콘 등록…',
                            lambda checked=False, key=kind: self.register_buff_icon(key))

        records = menu.addMenu('기록·백업')
        records.addAction('오늘·이번 주 요약', self.show_period_summary)
        records.addAction('기록 폴더 열기', self.open_records)
        records.addSeparator()
        records.addAction('백업 파일로 내보내기…', self.export_records)
        records.addAction('백업 파일에서 가져오기 (합치기)…', self.import_records)
        records.addSeparator()
        records.addAction('구글 드라이브 동기화 설정…', self.cloud_settings)
        if self.cloud_remote:
            records.addAction('지금 동기화', lambda: self.start_cloud_sync(manual=True))
            import cloud_sync
            info = records.addAction(cloud_sync.describe(cloud_sync.load_state()))
            info.setEnabled(False)

        help_menu = menu.addMenu('도움말·권한')
        help_menu.addAction('인식 정보', self.show_information)
        self.extra_menu_actions(help_menu)

        menu.addSeparator()
        menu.addAction('종료', self.close)
        return menu

    def register_buff_icon(self, kind):
        label = exp_buffs.LABELS[kind]
        path, _ = QFileDialog.getOpenFileName(
            self.window, f'{label} 아이콘 한 칸을 자른 이미지 선택', '', '이미지 (*.png *.jpg *.jpeg *.webp)')
        if not path:
            return
        try:
            exp_buffs.register(tracker.BASE, kind, path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self.window, '버프 아이콘 등록', str(exc))
            return
        self.buff_has_templates = True
        self.buff_revision += 1
        self.buff_confirmation = exp_buffs.Confirmation()
        self.buff_label.setText(f'{label} 등록 완료 · 측정 중 확인')

    def register_nickname(self):
        sample = self.nickname_sample
        if (sample is None or not sample[2] or sample[0] != self.character_source
                or self._monotonic() - sample[1] > 15):
            self.status.set('자동 인식으로 게임 HUD가 표시되면 닉네임을 등록할 수 있어요')
            return
        source, _, key = sample
        text, ok = QInputDialog.getText(
            self.window, '현재 닉네임 등록·수정', '게임 HUD에 보이는 닉네임을 입력해 주세요.',
            text=self.nickname_patterns.get(key) or self.character_identity[1] or self.current_character or '')
        if not ok:
            return
        name = text.strip()
        if not character_hud.valid_nickname(name):
            QMessageBox.warning(self.window, '닉네임 확인', '한글·영문·숫자 2~16자를 입력해 주세요.')
            return
        self.nickname_patterns = {**self.nickname_patterns, key: name}
        self.save_config()
        if self.nickname_sample and self.nickname_sample[0] == source and self.nickname_sample[2] == key:
            self.character_candidate = (self.character_candidate[0], name)
            self.set_detected_character((self.character_identity[0], name))

    def meso_threshold_settings(self):
        text, ok = QInputDialog.getText(
            self.window, '메소 큰 변동 기준',
            '잔액 차이가 이 금액을 넘으면 큰 변동으로 표시합니다.',
            text=f'{self.meso_trade_threshold:,}')
        if not ok:
            return
        try:
            value = int(text.strip().replace(',', ''))
            if not meso.MIN_THRESHOLD <= value <= meso.MAX_THRESHOLD:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self.window, '메소 큰 변동 기준', '1~1조 사이의 정수를 입력해 주세요.')
            return
        self.meso_trade_threshold = value
        self.save_config()

    def classify_meso_change(self):
        balance = self.session.meso
        if not balance.large_changes:
            self.status.set('이번 측정에서 분류할 큰 메소 변동이 없어요')
            return
        import record_history
        suggestions = {}
        if hasattr(self, 'meso_change_suggestion'):
            for idx, amount in enumerate(balance.large_changes):
                hint = self.meso_change_suggestion(idx, amount)
                if hint:
                    suggestions[idx] = hint
        result = record_history.classify_meso_dialog(
            self.window, balance, title='이번 측정의 큰 메소 변동 분류', suggestions=suggestions)
        if result is None or self.session.meso is not balance:
            return
        balance.marked = set(result)
        self.meso_prompt_session = self.session.session_id
        self.meso_prompt_reviewed = set(range(len(balance.large_changes)))
        self.session.meso_dirty = True
        if self.session.rows:
            try:
                self.save_summary()
            except OSError as exc:
                tracker.log_error('메소 변동 분류 저장', exc)
        self.update_meso_display(self._monotonic())

    def manage_potion_slots(self):
        slots = list(getattr(self, 'potion_slots', ()))
        if not slots:
            self.status.set('등록된 포션 슬롯이 없어요')
            return
        dialog = QDialog(self.window)
        dialog.setWindowTitle('등록된 포션 슬롯 관리')
        dialog.setStyleSheet('QDialog { background:#151b24; color:#e8f0f6; } QLabel { color:#d5dee8; }')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('단축키 포션 슬롯의 개당 가격을 수정하거나 삭제할 수 있어요.'))
        rows_box = QVBoxLayout()
        layout.addLayout(rows_box)

        def rebuild():
            while rows_box.count():
                item = rows_box.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    while item.layout().count():
                        sub = item.layout().takeAt(0)
                        if sub.widget():
                            sub.widget().deleteLater()
            if not slots:
                rows_box.addWidget(QLabel('등록된 포션 슬롯이 없어요.'))
                return
            for idx, slot in enumerate(slots):
                row = QHBoxLayout()
                cost = slot.get('unit_cost', 0)
                info = QLabel(f"{idx + 1}. {slot['name']} · 개당 {cost:,} 메소" if cost else f"{idx + 1}. {slot['name']} · 단가 미지정")
                row.addWidget(info, 1)
                edit_btn = QPushButton('단가 수정')
                del_btn = QPushButton('삭제')

                def edit_cost(i=idx):
                    val, ok = QInputDialog.getInt(
                        dialog, '포션 단가 수정', f"{slots[i]['name']} 1개당 가격 (메소, 0=미계산)",
                        int(slots[i].get('unit_cost', 0)), 0, 10**9)
                    if ok:
                        slots[i] = dict(slots[i], unit_cost=val)
                        rebuild()

                def remove_slot(i=idx):
                    slots.pop(i)
                    rebuild()

                edit_btn.clicked.connect(lambda _=False, fn=edit_cost: fn())
                del_btn.clicked.connect(lambda _=False, fn=remove_slot: fn())
                row.addWidget(edit_btn)
                row.addWidget(del_btn)
                rows_box.addLayout(row)

        rebuild()
        actions = QHBoxLayout()
        actions.addStretch()
        save_btn = QPushButton('저장')
        cancel_btn = QPushButton('닫기')
        save_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        actions.addWidget(save_btn)
        actions.addWidget(cancel_btn)
        layout.addLayout(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            import potions
            kept_icons = {s.get('icon') for s in slots if s.get('icon')}
            for old in getattr(self, 'potion_slots', ()):
                if old.get('icon') and old['icon'] not in kept_icons:
                    potions.delete_icon(old['icon'], tracker.BASE)
            self.potion_slots = list(slots)
            self.potion_reader = potions.Reader()
            self.potion_confirmations = {}
            self.save_config()
            self.update_potion_display(self._monotonic())
            self.status.set(f'포션 슬롯 {len(self.potion_slots)}개 저장됨' if self.potion_slots else '포션 슬롯 모두 삭제됨')

    def show_history(self):
        from record_history import build_dialog
        self.history_dialog = build_dialog(self)
        self.history_dialog.show()
        self.history_dialog.raise_()

    def choose_character(self):
        """New records carry this name unless OCR reads one or the record is edited later."""
        import record_history
        known = record_history.known_characters()
        current = self.current_character or ''
        options = [current] + [name for name in known if name != current] if current else ([''] + known)
        text, ok = QInputDialog.getItem(
            self.window, '현재 캐릭터',
            '지금 사냥하는 캐릭터 (새 기록에 붙어요 · 비우면 지정 해제)', options, 0, True)
        if not ok:
            return
        name = ' '.join(text.split())[:24] or None
        self.current_character = name
        self.save_config()
        if self.running and self.session.rows and not self.session.restored:
            try:
                self.save_summary()  # the record being measured gets the name too
            except OSError as exc:
                tracker.log_error('현재 캐릭터 저장', exc)
        self.status.set(f'현재 캐릭터 · {name}' if name else '현재 캐릭터 지정 해제')

    def show_period_summary(self):
        import record_history
        try:
            text = record_history.period_text(record_history.collect())
        except Exception as exc:
            tracker.log_error('사냥 요약', exc)
            text = '요약을 만들지 못했어요. last-error.log를 확인해 주세요.'
        QMessageBox.information(self.window, '오늘·이번 주 요약', text)

    def api_key_settings(self):
        """Enter the LLM Gateway key used by JEV; it is kept only in the OS key store."""
        import map_name
        has_key = map_name.gateway_key() is not None
        text, ok = QInputDialog.getText(
            self.window, 'JEV API 키',
            ('현재: 저장됨' if has_key else '현재: 없음') + '\n\n'
            'LLM Gateway API 키를 붙여넣어 주세요.\n'
            '이 컴퓨터의 키 저장소(맥 키체인 / 리눅스 키링)에만 저장되고,\n'
            '파일이나 GitHub에는 올라가지 않아요.\n'
            '비워 두고 확인하면 저장된 키를 지워요.',
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        if not text.strip() and not has_key:
            return
        try:
            map_name.store_key(text)
        except OSError as exc:
            tracker.log_error('JEV API 키 저장', exc)
            QMessageBox.warning(self.window, 'JEV API 키', f'저장하지 못했어요.\n{exc}')
            return
        saved = map_name.gateway_key() is not None
        self.status.set('JEV API 키 저장됨' if saved else 'JEV API 키 삭제됨')

    def open_records(self):
        self.open_path(tracker.BASE / 'records')
