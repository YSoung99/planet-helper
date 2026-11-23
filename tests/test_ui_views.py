import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import tracker
from mac_app import Controller


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=patch.object(tracker,'BASE',Path(self.temp.name));self.base.start()
        self.c=Controller(self.app,start_worker=False,restore=False);self.c.timer.stop()
    def tearDown(self):
        self.c.key_repeat.shutdown();self.c.start_captures.close();self.c.window.hide();self.c.window.deleteLater()
        self.app.processEvents();self.base.stop();self.temp.cleanup()
    def test_compact_full_transition_preserves_current_measurement_and_settings(self):
        session=self.c.session;helper=self.c.key_repeat
        self.assertTrue(self.c.compact);self.assertEqual(self.c.window.width(),360)
        self.assertLess(self.c.window.height(),205)
        self.c.toggle_display();self.assertFalse(self.c.compact);self.assertEqual(self.c.window.width(),640)
        self.assertLess(self.c.window.height(),480)  # two-column detailed view stays short
        self.assertIs(self.c.session,session);self.assertIs(self.c.key_repeat,helper)
        self.c.toggle_pin(False)
        clean,warning=tracker.load_settings(tracker.BASE/'settings.json')
        self.assertFalse(clean['always_on_top']);self.assertFalse(clean['compact']);self.assertIsNone(warning)
        self.c.toggle_display();self.assertLess(self.c.window.height(),205)

    def test_meso_readings_reach_the_log_session_and_detail_line(self):
        c = self.c
        c.toggle_display()
        self.assertTrue(c.meso_label.isVisibleTo(c.window))
        self.assertTrue(c.meso_delta_label.isVisibleTo(c.window))
        self.assertIs(c.meso_delta_label.parentWidget(), c.total_label.parentWidget())
        self.assertEqual(c.meso_delta_label.text(), '—')
        c.running = True
        c.current_character = '본캐'
        c.session.meso_start_at = 100.0
        for stamp, value in ((102, 1_000_000), (104, 1_000_000), (106, 1_000_000),
                             (120, None), (124, 3_250_000), (126, 3_250_000)):
            c.meso_results.put((c.generation, 20, stamp, '본캐', value))
            c.apply_meso_results(stamp)
        c.meso_results.put((c.generation, 20, 127, '부캐', 999))
        c.apply_meso_results(127)
        self.assertEqual(c.meso_label.text(), '메소 3,250,000 · 큰 변동 1건')
        self.assertEqual(c.meso_delta_label.text(), '+2,250,000')
        c.session.meso.toggle_trade(0)
        c.apply_meso_results(127)
        self.assertEqual(c.meso_delta_label.text(), '+0')
        c.session.meso.toggle_trade(0)
        import meso
        self.assertEqual([value for _, _, value in meso.load(tracker.BASE)], [1_000_000, 3_250_000])
        self.assertIsNone(c.session.meso.hunted())
        with patch('qt_controller.time.monotonic', return_value=128):
            self.assertTrue(c.finish_measurement('사용자 정지'))
        self.assertEqual(c.session.meso.hunted(), 2_250_000)
        c.toggle_display()
        self.assertFalse(c.meso_label.isVisibleTo(c.window))

    def test_meso_confirmation_capture_ignores_previous_generation_cadence(self):
        from PIL import Image
        from unittest.mock import Mock

        c = self.c
        c.auto_hud = False
        c.running = True
        c.session.meso_start_at = 100.0
        c.capture_clients.meso_generation = c.generation - 1
        c.capture_clients.next_meso_scan = 900.0
        full = Image.new('RGB', (800, 600))
        exp = Image.new('RGB', (150, 30))
        exp.info.update(window_id=20, full_game_image=full)
        c.capture_clients.client = Mock()
        c.capture_clients.client.snapshot.return_value = exp
        with c.hunt_activity_lock, patch('qt_controller.time.monotonic', return_value=101.0), patch(
            'map_name.available', return_value=True
        ), patch.object(c, 'queue_meso_frame', return_value=True) as queued:
            self.assertIs(c.capture_frame({}), exp)
        queued.assert_called_once_with(c.generation, 101.0, full, 20)
        self.assertTrue(c.capture_clients.client.snapshot.call_args.kwargs['include_full'])
        self.assertEqual(c.capture_clients.next_meso_scan, 102.0)

    def test_accepted_exp_baseline_queues_its_full_frame_for_meso(self):
        from PIL import Image

        c = self.c
        c.running = True
        c.session = tracker.Session(level=170)
        full = Image.new('RGB', (800, 600))
        stamp = c.session.created_monotonic + 1
        details = {'window_id': 20, 'full_game_image': full, 'auto_detected': True,
                   'auto_confirmed': True, 'detected_level': 170, 'temporal': {'selected': 1}}
        c.events.put((c.generation, (219671043, 51.32), 'EXP', .99, None, stamp, details))
        with patch('map_name.available', return_value=True), patch.object(
            c, 'queue_meso_frame', return_value=True
        ) as queued:
            c.poll()
        self.assertEqual(c.session.first, (219671043, 51.32))
        self.assertEqual(c.session.meso_start_at, stamp)
        queued.assert_called_once_with(c.generation, stamp, full, 20)
        self.assertIsNone(c.session.meso.start)

    def test_busy_reader_retries_the_selected_frame_and_no_inventory_keeps_exp_running(self):
        from PIL import Image

        c = self.c
        c.running = True
        c.session = tracker.Session(level=170)
        full = Image.new('RGB', (800, 600))
        stamp = c.session.created_monotonic + 1
        with patch('map_name.available', return_value=True), patch.object(
            c, 'queue_meso_frame', side_effect=[False, True]
        ) as queued:
            c.start_meso_capture(c.generation, stamp, {'window_id': 20, 'full_game_image': full})
            self.assertIsNotNone(c.session.meso_start_frame)
            c.apply_meso_results(stamp + 1)
        self.assertEqual(queued.call_count, 2)
        self.assertEqual(queued.call_args.args, (c.generation, stamp, full, 20))
        self.assertIsNone(c.session.meso_start_frame)
        c.meso_results.put((c.generation, 20, stamp, '', None))
        c.apply_meso_results(stamp + 1)
        self.assertTrue(c.running)
        self.assertIsNone(c.session.meso.start)
        self.assertEqual(c.meso_delta_label.text(), '—')

    def test_starting_balance_uses_first_of_matching_captures(self):
        c = self.c
        c.running = True
        c.current_character = '본캐'
        c.session.meso_start_at = 100.0
        c.meso_results.put((c.generation, 20, 129.0, '본캐', 1_000_000))
        c.apply_meso_results(129.0)
        self.assertIsNone(c.session.meso.start)
        c.meso_results.put((c.generation, 20, 131.0, '본캐', 1_000_000))
        c.apply_meso_results(131.0)
        self.assertEqual(c.session.meso.start, 1_000_000)
        self.assertEqual(c.session.meso.last_seen, 131.0)
        self.assertEqual(c.meso_status_label.text(), '시작 잔액 확인 · 종료 잔액 대기')

    def test_meso_ocr_disagreement_holds_the_last_value_until_a_real_decrease_is_confirmed(self):
        import meso
        c = self.c
        c.running = True
        c.session.meso_start_at = 100.0
        for stamp, value in ((101, 100), (102, 100), (103, 110), (104, 110),
                             (105, meso.RECHECK)):
            c.meso_results.put((c.generation, 20, stamp, '', value))
            c.apply_meso_results(stamp)
        self.assertEqual(c.meso_delta_label.text(), '+10')
        self.assertEqual(c.meso_label.text(), '메소 110 · 잔액 재확인 중')
        self.assertEqual(c.meso_status_label.text(), '시작 잔액 확인 · 종료 잔액 대기')
        self.assertEqual([amount for _, _, amount in meso.load(tracker.BASE)], [100, 110])
        for stamp in (106, 107):
            c.meso_results.put((c.generation, 20, stamp, '', 95))
            c.apply_meso_results(stamp)
        self.assertEqual(c.meso_delta_label.text(), '-5')
        self.assertEqual(c.meso_status_label.text(), '시작 잔액 확인 · 종료 잔액 확인')

    def test_meso_changes_are_confirmed_on_the_regular_capture_cadence(self):
        from PIL import Image, ImageDraw
        import meso

        c = self.c
        c.running = True
        c.session.meso_start_at = 100.0
        with patch.object(c, 'capture_module') as extra_capture, \
             patch('map_name.recognize_lines', return_value=[]), \
             patch('meso.has_label', return_value=True), \
             patch('meso.read_amount', side_effect=[100, 95, 99]), \
             patch('meso.read_binary_amount', side_effect=[100, 95, 99]):
            for stamp, amount, confirmed in (
                (101, 100, None), (103, 100, 100), (105, 95, 100), (107, 99, 100), (109, 99, 99)
            ):
                frame = Image.new('RGB', (400, 300), (40, 60, 90))
                draw = ImageDraw.Draw(frame)
                draw.rectangle((50, 75, 199, 92), fill=(250, 250, 250))
                draw.text((172, 79), str(amount), fill=(0, 0, 0))
                c.meso_capture_lock.acquire()
                c.read_meso_frame(c.generation, stamp, frame, 20, '')
                self.assertFalse(c.meso_capture_lock.locked())
                c.apply_meso_results(stamp)
                self.assertEqual(c.session.meso.end, confirmed)
        extra_capture.assert_not_called()
        self.assertEqual([value for _, _, value in meso.load(tracker.BASE)], [100, 99])
        self.assertEqual(c.meso_delta_label.text(), '-1')

    def test_meso_confirmation_status_expires_before_stop(self):
        c = self.c
        c.running = True
        c.meso_source = (c.generation, '본캐', 20)
        balance = c.session.meso
        balance.sample(100, c.meso_source, 102, 1, 100, 100)
        self.assertEqual(c.meso_status(103), '시작 잔액 확인 · 종료 잔액 대기')
        balance.sample(200, c.meso_source, 130, 2, 100, 100)
        self.assertEqual(c.meso_status(131), '시작 잔액 확인 · 종료 잔액 확인')
        self.assertEqual(c.meso_status(143), '시작 잔액 확인 · 종료 잔액 대기')

    def test_connected_nickname_registration_and_character_change(self):
        c = self.c
        c.set_character_source((c.generation, 20))
        unknown = 'a' * 32
        for stamp in (100, 102):
            c.character_results.put((c.generation, 20, stamp, '섀도어', None, unknown))
            c.apply_character_results(stamp)
        self.assertIn('확인 대기', c.source_label.text())
        with patch('qt_controller.time.monotonic', return_value=103), patch(
                'PySide6.QtWidgets.QInputDialog.getText', return_value=('테스트본캐', True)):
            c.register_nickname()
        self.assertIn('접속 닉네임 테스트본캐', c.source_label.text())
        clean, warning = tracker.load_settings(tracker.BASE / 'settings.json')
        self.assertIsNone(warning)
        self.assertEqual(clean['nickname_patterns'], {unknown: '테스트본캐'})
        other = 'b' * 32
        c.character_results.put((c.generation, 20, 105, '아란', '테스트부캐', other))
        c.apply_character_results(105)
        self.assertIn('확인 대기', c.source_label.text())
        c.character_results.put((c.generation, 20, 107, '아란', '테스트부캐', other))
        c.apply_character_results(107)
        self.assertIn('접속 닉네임 테스트부캐', c.source_label.text())
        c.apply_character_results(123)
        self.assertIn('확인 대기', c.source_label.text())

    def test_meso_threshold_accepts_a_value_above_qt_integer_range(self):
        with patch('PySide6.QtWidgets.QInputDialog.getText', return_value=('1,000,000,000,000', True)):
            self.c.meso_threshold_settings()
        self.assertEqual(self.c.meso_trade_threshold, 10**12)

    def test_level_eta_shows_arrival_clock(self):
        from datetime import datetime
        from types import SimpleNamespace
        from qt_controller import level_eta_text
        session=SimpleNamespace(level=150,total=1_000_000,last=(400_000,0),level_ups=[])
        rate={'status':'ready','cumulative':2_500}
        now=datetime(2026,11,20,13,30)
        self.assertEqual(level_eta_text(rate,session,compact=True,now=now),'Lv.150 시간당 15만 · 레벨업 4시간 0분 (17:30)')
        self.assertIn('레벨업까지 4시간 0분 · 17:30쯤',level_eta_text(rate,session,now=now))
        late=datetime(2026,11,20,22,0)
        self.assertIn('(내일 02:00)',level_eta_text(rate,session,compact=True,now=late))
        rate['cumulative']=100
        self.assertIn('(11/24 17:30)',level_eta_text(rate,session,compact=True,now=now))

    def test_detailed_view_keeps_controls_for_the_active_recognition_mode(self):
        c = self.c
        c.toggle_display()
        self.assertFalse(c.region_button.isVisibleTo(c.window))
        self.assertTrue(c.auto_checkbox.isVisibleTo(c.window))
        c.auto_checkbox.setChecked(False)
        self.assertTrue(c.region_button.isVisibleTo(c.window))
        self.assertTrue(c.level_picker.isEnabled())
        c.auto_checkbox.setChecked(True)
        self.assertFalse(c.region_button.isVisibleTo(c.window))

    def test_pin_button_is_reachable_in_both_views(self):
        c=self.c
        self.assertTrue(c.compact);self.assertTrue(c.pin_button.isVisibleTo(c.window))
        self.assertEqual(c.header_layout.indexOf(c.pin_button),c.header_layout.indexOf(c.mode_button)-1)
        c.pin_button.click();self.assertFalse(c.always_on_top);self.assertEqual(c.pin_button.text(),'맨 위 끔')
        c.toggle_display()
        self.assertTrue(c.pin_button.isVisibleTo(c.window))
        self.assertEqual(c.header_layout.indexOf(c.pin_button), c.header_layout.indexOf(c.mode_button)-1)

    def test_compact_view_keeps_new_measurement_button(self):
        c=self.c
        self.assertTrue(c.compact);self.assertTrue(c.reset_button.isVisibleTo(c.window))
        self.assertEqual(c.repeat_controls.indexOf(c.reset_button),1)
        c.toggle_display()
        self.assertEqual(c.measurement_controls.indexOf(c.reset_button),1)
        c.toggle_display()
        self.assertEqual(c.repeat_controls.indexOf(c.reset_button),1)

    def test_jev_status_belongs_to_the_current_measurement_in_both_views(self):
        c = self.c
        session_id = c.session.session_id
        name = '사자왕의 성 · 낮은 성벽3'
        self.assertTrue(c.jev_status.isHidden())
        self.assertFalse(c.jev_button.isChecked())
        c.jev_button.click()
        revision = c.jev_revision
        self.assertTrue(tracker.load_settings(tracker.BASE/'settings.json')[0]['jev_enabled'])
        c.map_results.put((session_id, revision, 'JEV 확인 중…'))
        c.poll()
        self.assertFalse(c.jev_status.isHidden())
        self.assertEqual(c.jev_status.text(), 'JEV 확인 중…')
        c.toggle_display()
        c.map_results.put((session_id, revision, ('낮은 성벽스', name)))
        c.poll()
        self.assertFalse(c.jev_status.isHidden())
        self.assertEqual(c.jev_status.text(), f'JEV로 확인 · {name}')
        c.jev_button.click()
        c.jev_button.click()
        c.map_results.put((session_id, revision, 'JEV 확인 중…'))
        c.map_results.put((session_id, revision, ('낮은 성벽스', name)))
        c.poll()
        self.assertTrue(c.jev_status.isHidden())
        c.reset()
        c.map_results.put((session_id, c.jev_revision, 'JEV 확인 중…'))
        c.poll()
        self.assertTrue(c.jev_status.isHidden())

    def test_jev_switch_gates_network_without_disabling_local_map_reading(self):
        from PIL import Image
        from record_history import load_memos
        c = self.c
        frame = Image.new('RGB', (120, 120))

        def read_map():
            c.start_map_reading(c.session.session_id, frame)
            c.apply_map_results()

        with patch('qt_controller.threading.Thread') as worker, \
                patch('map_name.available', return_value=True), \
                patch('map_name.read_title', return_value='기기에서 읽은 이름'), \
                patch('map_name.choose') as choose:
            worker.side_effect = lambda *, target, **kwargs: type(
                'Worker', (), {'start': staticmethod(target)})()
            read_map()
            choose.assert_not_called()
            self.assertEqual(load_memos()[c.session.session_id], '기기에서 읽은 이름')
            c.reset()
            c.jev_button.click()
            choose.side_effect = lambda _raw, _title, *, on_request: on_request(False) and None
            read_map()
            choose.assert_called_once()
            self.assertIn('API 키 필요', c.jev_status.text())
            self.assertEqual(load_memos()[c.session.session_id], '기기에서 읽은 이름')

    def test_recent_button_starts_a_new_saved_measurement_and_accepts_live_exp(self):
        c = self.c
        c.recent_permission = True
        original = c.session.session_id
        source = (c.generation, 7, 169, (1600, 900), 'classic')
        for stamp, exp in ((100, 172680132), (102, 172680232), (104, 172680332)):
            c.events.put((c.generation, (exp, 42.55), '', .99, None, stamp,
                          {'recent': True, 'source': source}))
        with patch('time.monotonic', return_value=105):
            c.poll()
            self.assertTrue(c.recent_button.isEnabled())
            self.assertFalse(list((tracker.BASE/'records').glob('*.csv')))
            c.recent_button.click()
            self.assertTrue(c.running)
            self.assertNotEqual(c.session.session_id, original)
            self.assertEqual(c.session.stats(105)[0], 200)
            c.events.put((c.generation, (172680432, 42.55), '', .99, None, 105,
                          {'detected_level': 169, 'window_id': 7, 'content_points': (1600, 900),
                           'ui_kind': 'classic', 'temporal': {'selected': 1}}))
            c.poll()
            self.assertEqual(c.session.stats(105)[0], 300)
            self.assertTrue(c.recent_button.isHidden())
            c.finish_measurement('사용자 정지')
        path = tracker.BASE/'records'/(c.session.session_id+'.csv')
        restored = tracker.load_saved_session(path, 169, 110)
        self.assertEqual(restored.stats(110)[0], 300)

    def test_rate_display_changes_on_ten_second_observation_buckets_only(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        s.accept((172680232,42.55),3);c.update_rate_display(3)
        self.assertEqual(c.minute_label.text(),'집계 중')
        for stamp in range(4,62):s.accept((172680232,42.55),stamp)
        c.update_rate_display(61);self.assertEqual(c.minute_label.text(),'100')
        self.assertEqual(c.compact_average.text(),'100')
        s.accept((172680332,42.55),62);c.update_rate_display(62)
        self.assertEqual(c.minute_label.text(),'100')
        for stamp in range(63,72):s.accept((172680332,42.55),stamp)
        c.update_rate_display(71);self.assertEqual(c.minute_label.text(),'171')
        self.assertEqual(c.compact_average.text(),'171')
        c.update_rate_display(78);self.assertEqual(c.minute_label.text(),'인식 지연')

    def test_stopped_rate_reappears_with_asof_and_gap_even_in_same_display_bucket(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        for stamp in range(2,62):s.accept((172680232,42.55),stamp)
        c.update_rate_display(70)
        self.assertEqual(c.minute_label.text(),'인식 지연')
        s.finish(200,'사용자 정지');c.update_rate_display(200)
        self.assertEqual(c.minute_label.text(),'100')
        self.assertEqual(c.compact_average.text(),'100')
        self.assertIn('마지막 확인 0:59',c.rate_info.text())
        self.assertIn('139초 관측 공백',c.rate_info.text())

    def test_footer_shows_current_recording_gap_until_experience_is_verified_again(self):
        c = self.c
        c.running = True
        generation = c.generation
        details = {'detected_level': 169, 'auto_detected': True, 'auto_confirmed': True,
                   'ui_kind': 'classic', 'temporal': {'selected': 1}}
        c.events.put((generation, (172680132, 42.55), '', .99, None, 100, details))
        c.events.put((generation, (172680232, 42.55), '', .99, None, 101, details))
        with patch('time.monotonic', return_value=111):
            c.poll()
        self.assertIn('인식 지연', c.footer.text())

        error = '레벨 표시 확인 중 · LV와 숫자가 보이게 해 주세요'
        c.events.put((generation, None, '', 0, error, 112, {}))
        with patch('time.monotonic', return_value=166):
            c.poll()
        self.assertIn('기록 공백 1:05', c.footer.text())
        self.assertIn(error, c.footer.toolTip())
        self.assertIn('172,680,232', c.current_label.text())

        c.events.put((generation, (172680532, 42.55), '', .99, None, 167, details))
        with patch('time.monotonic', return_value=167):
            c.poll()
        self.assertIn('갱신', c.footer.text())
        self.assertEqual(c.session.gap_closed, 66)
        self.assertEqual(c.session.gap_gain, 300)
        self.assertIn('172,680,532', c.current_label.text())

    def test_both_views_show_the_same_time_and_six_minute_statistics(self):
        c = self.c
        session = c.session
        session.accept((172680132,42.55),0)
        session.accept((172680132,42.55),1)
        for stamp in range(2,68,5):
            session.accept((172680132+(stamp-1)*100,42.55),stamp)
        with patch('time.monotonic',return_value=67):
            c.poll()
        self.assertEqual(c.compact_time.text(), c.timer_label.text())
        self.assertEqual(c.compact_six_heading.text(), '6분 예상 · 1/6분')
        self.assertEqual(c.compact_recent_six.text(), '36,000')
        self.assertEqual(c.compact_recent_six.text(), c.recent_six.text())
        self.assertEqual(c.compact_recent_minute.text(), c.recent_minute.text())
        self.assertNotIn(c.compact_recent_minute.text(), ('—', ''))
        for stamp in range(72,368,5):
            session.accept((172680132+(stamp-1)*100,42.55),stamp)
        with patch('time.monotonic',return_value=367):
            c.poll()
        self.assertEqual(c.compact_six_heading.text(), '최근 6분 획득')
        self.assertEqual(c.compact_recent_six.text(), c.recent_six.text())
        self.assertEqual(c.compact_recent_six.text(), '36,000')
        for stamp in range(387,428,5):
            session.accept((172680132+(stamp-1)*100,42.55),stamp)
        with patch('time.monotonic',return_value=427):
            c.poll()
        # A 20s observation gap: its time and its EXP are both left out of the window.
        self.assertEqual(c.compact_recent_six.text(), c.recent_six.text())
        self.assertEqual(c.compact_recent_six.text(), '36,000')

    def test_worker_corroboration_reaches_small_decrease_safe_stop(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        s.accept((172685132,42.55),2)
        c.running=True
        from temporal_ocr import verify
        def f(value,stamp):
            return dict(value=value,score=.99,raw='fixture',observed_at=stamp)
        # A real rejected burst must not be delivered as a corroborated value.
        first=f((172680132,42.55),2.1)
        captures=iter([f(None,2.2),f(None,2.3)])
        selected,evidence,_=verify(first,lambda force:next(captures),s.total,s.last,
                                   lambda:False,lambda delay:False,level=s.level)
        self.assertIsNone(selected)
        c.events.put((c.generation,None,'fixture',.99,None,2.3,{'temporal':evidence}))
        c.poll();self.assertEqual(s.small_decrease_count,0)
        for stamp in (3,4,5):
            first=f((172680132,42.55),stamp)
            selected,evidence,_=verify(first,lambda force:first,s.total,s.last,
                                       lambda:False,lambda delay:False,level=s.level)
            self.assertIsNotNone(selected)
            c.events.put((c.generation,selected['value'],'fixture',.99,None,stamp,{'temporal':evidence}))
        c.poll()
        self.assertTrue(s.blocked)
        self.assertFalse(c.running)
        self.assertIsNotNone(s.ended)
        self.assertEqual(s.stats(6)[0],5000)
        self.assertIn('감소 충돌',c.footer.text())
        self.assertIn('확인 필요',c.total_label.toolTip())
        c.save_warning='자동 저장 실패'
        c.audit_warning='진단 저장 실패'
        c.poll()
        c.window.show();self.app.processEvents()
        self.assertTrue(c.footer.isVisibleTo(c.window))
        self.assertIn('감소 충돌',c.footer.text())
        self.assertIn('자동 저장 실패',c.footer.toolTip())

    def feed_recent(self, points, source=None):
        c = self.c
        source = source or (c.generation, 7, 169, (1600, 900), 'classic')
        for stamp, exp in points:
            c.events.put((c.generation, (exp, 42.55), '', .99, None, stamp,
                          {'recent': True, 'source': source}))

    def live(self, stamp, exp):
        c = self.c
        c.events.put((c.generation, (exp, 42.55), '', .99, None, stamp,
                      {'detected_level': 169, 'window_id': 7, 'content_points': (1600, 900),
                       'ui_kind': 'classic', 'temporal': {'selected': 1}}))

    def test_automatic_measurement_starts_on_repeated_gains_and_ends_after_long_pause(self):
        c = self.c
        c.recent_permission = True
        base = 172680132
        self.feed_recent([(100, base), (102, base+100)])
        with patch('time.monotonic', return_value=103):
            c.poll()
            self.assertFalse(c.running)  # switched off by default
            c.set_auto_measure(True)
            self.assertTrue(tracker.load_settings(tracker.BASE/'settings.json')[0]['auto_measure'])
            c.poll()
            self.assertFalse(c.running)  # one gain can be a quest reward
        self.feed_recent([(104, base+200)])
        with patch('time.monotonic', return_value=105):
            c.poll()
            self.assertTrue(c.running)
            self.assertEqual(c.session.stats(105)[0], 200)
            first = c.session.session_id
        for stamp in range(107, 104+int(c.AUTO_END_SECONDS), 4):
            self.live(stamp, base+200)
        with patch('time.monotonic', return_value=104+c.AUTO_END_SECONDS-1):
            c.poll()
            self.assertTrue(c.running)
        end = 104+c.AUTO_END_SECONDS+1
        self.live(end, base+200)
        with patch('time.monotonic', return_value=end):
            c.poll()
        self.assertFalse(c.running)
        self.assertIn('자동 종료', c.session.stop_reason)
        self.assertEqual(c.session.session_id, first)
        self.assertTrue((tracker.BASE/'records'/(first+'.csv')).exists())

    def test_manual_stop_holds_automatic_start_until_exp_pauses(self):
        c = self.c
        c.recent_permission = True
        c.set_auto_measure(True)
        base = 172680132
        self.feed_recent([(100, base), (102, base+100), (104, base+200)])
        with patch('time.monotonic', return_value=105):
            c.poll()
            self.assertTrue(c.running)
            c.toggle()
            self.assertFalse(c.running)
        stopped = c.session.session_id
        self.feed_recent([(106, base+300), (108, base+400), (110, base+500)])
        with patch('time.monotonic', return_value=111):
            c.poll()
        self.assertFalse(c.running)  # still hunting right after the manual stop
        self.feed_recent([(stamp, base+500) for stamp in range(112, 176, 4)])
        with patch('time.monotonic', return_value=173):
            c.poll()
        self.assertFalse(c.running)
        self.feed_recent([(174, base+500), (176, base+600), (178, base+700)])
        with patch('time.monotonic', return_value=179):
            c.poll()
        self.assertTrue(c.running)
        self.assertNotEqual(c.session.session_id, stopped)

    def test_turning_off_recent_collection_turns_off_automatic_measurement(self):
        c = self.c
        c.recent_recording = False
        with patch.object(type(c), 'has_background_capture_permission', return_value=True):
            c.set_auto_measure(True)
            self.assertTrue(c.recent_recording)
            c.set_recent_recording(False)
        self.assertFalse(c.auto_measure)
        self.assertFalse(tracker.load_settings(tracker.BASE/'settings.json')[0]['auto_measure'])
