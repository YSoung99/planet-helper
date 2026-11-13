"""Offline packaged checks: real disabled helper, local OCR and isolated records."""
import json
import os
import platform
import time
from pathlib import Path
from unittest.mock import patch


def run_offline_diagnostics(application, report_path):
    from PIL import Image
    from PySide6.QtCore import QProcess, qVersion
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    import tracker
    from mac_app import Controller

    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = dict(version=tracker.APP_VERSION, frozen=bool(getattr(__import__('sys'), 'frozen', False)),
                  python=platform.python_version(), qt=qVersion(), data_path=str(tracker.BASE),
                  network_blocked=True, permissions_requested=False, repetition_enabled=False)
    controller = None
    try:
        controller = Controller(application, start_worker=False, restore=False, start_repeat=True)
        controller.show()
        def wait_until(predicate, message, timeout=8):
            deadline = time.monotonic()+timeout
            while not predicate():
                application.processEvents()
                if time.monotonic() >= deadline:
                    raise AssertionError(message)
                QTest.qWait(20)
            application.processEvents()
        wait_until(lambda: controller.key_repeat.state.get('type') == 'status',
                   'Native helper did not return a status handshake')
        bridge = controller.key_repeat
        report['helper'] = dict(bridge.state)
        report['helper']['pid'] = int(bridge.process.processId())
        assert bridge.process.state() == QProcess.ProcessState.Running, report
        assert bridge.state['enabled'] is False and bridge.state['active'] is False, report
        assert bridge.state['settings']['attackTriggerKeyCode'] == 0, report
        assert not bridge.state.get('error'), report
        initial = dict(bridge.settings)
        changed = dict(initial, firstDelayMs=initial['firstDelayMs']+1 if initial['firstDelayMs'] < 2000 else 1999)
        bridge.configure(changed)
        wait_until(lambda: bridge.state.get('settings') == changed,
                   'Disabled native settings command was not acknowledged')
        assert bridge.state['enabled'] is False and bridge.state['active'] is False, report
        bridge.configure(initial)
        wait_until(lambda: bridge.state.get('settings') == initial,
                   'Original native settings were not restored')
        report['settings_roundtrip_disabled'] = True
        assert json.loads(bridge.settings_path.read_text()) == initial, report
        report['settings_persisted'] = True

        window = controller.window
        application.processEvents()
        controls = (controller.toggle_button, controller.repeat_button, controller.repeat_settings_button)
        report['ui'] = dict(title=window.windowTitle(), width=window.width(), height=window.height(),
                            measurement_label=controller.toggle_button.text(),
                            repeat_label=controller.repeat_button.text())
        for widget in controls:
            assert window.rect().contains(widget.geometry()), (widget.text(), widget.geometry(), report)
            assert widget.width() >= widget.fontMetrics().horizontalAdvance(widget.text())+6, report
        assert controller.toggle_button.text() == '측정 시작', report
        assert controller.repeat_button.text() == '연타 켜기', report
        report['ui']['control_bounds_valid'] = True
        from mac_app import native_overlay
        import ctypes
        import objc
        def native_level():
            view=objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
            return int(view.window().level())
        original_session=controller.session
        original_helper=bridge.process.processId()
        report['display']={'compact_size':[window.width(),window.height()],
                           'background_alpha':window.grab().toImage().pixelColor(5,45).alpha(),
                           'text_opacity':window.windowOpacity()}
        assert window.width()==360 and window.height()<180, report
        assert 220 <= report['display']['background_alpha'] <= 235, report
        assert window.windowOpacity()==1.0, report
        controller.toggle_pin(False);application.processEvents()
        report['display']['unpinned_native_level']=native_level()
        assert native_level()==0, report
        controller.toggle_display();application.processEvents()
        report['display']['full_size']=[window.width(),window.height()]
        window.grab().save(str(report_path.with_name(report_path.stem+'-full.png')))
        assert window.width()==374 and window.height()>400, report
        assert controller.session is original_session and bridge.process.processId()==original_helper, report
        clean,_=tracker.load_settings(tracker.BASE/'settings.json')
        assert clean['compact'] is False and clean['always_on_top'] is False, report
        controller.toggle_display();controller.pin_button.setChecked(True);application.processEvents()
        report['display']['pinned_native_level']=native_level()
        assert native_level()==3, report
        window.grab().save(str(report_path.with_suffix('.png')))

        with patch('requests.sessions.Session.request', side_effect=RuntimeError('Offline verification')):
            engine = tracker.create_ocr_engine()
            report['ocr'] = []
            fixtures = [('0.4.4_인식복구.png', (395571663, 97.48)),
                        ('new-ui-exp.png', (34514347, 8.06)),
                        ('new-ui-label-dot.png', (40148011, 9.38))]
            for filename, expected in fixtures:
                with Image.open(tracker.RESOURCES/'verification'/filename) as image:
                    reader = tracker.LineReader(engine)
                    value, score, raw, _ = reader.read(image.convert('RGB'), time.monotonic())
                report['ocr'].append(dict(fixture=filename, value=value, expected=expected,
                                          score=float(score), matches=value == expected, raw=raw))
                assert value == expected, report

        controller.session = tracker.Session(level=169)
        controller.running = True
        for stamp, reading in ((0, (172680132,42.55)), (1, (172680132,42.55)),
                               (3, (172680232,42.55)), (63, (172683232,42.55))):
            controller.events.put((controller.generation, reading, str(reading), .99, None, stamp, {}))
            controller.poll()
        report['ledger'] = dict(gained=controller.session.stats(63)[0],
                                label=controller.total_label.text(),
                                csv_exists=(tracker.BASE/'records'/(controller.session.session_id+'.csv')).is_file())
        assert report['ledger'] == dict(gained=3100,label='3,100',csv_exists=True), report
        assert bridge.state['enabled'] is False and bridge.state['active'] is False, report
        report['measurement_does_not_enable_repeat'] = True
        report['shutdown_succeeded'] = controller.shutdown()
        assert report['shutdown_succeeded'], report
        application.processEvents()
        report['helper_exited'] = bridge.process.state() == QProcess.ProcessState.NotRunning
        report['capture_writer_exited'] = not controller.start_captures.thread.is_alive()
        report['ui_timer_stopped'] = not controller.timer.isActive()
        assert report['helper_exited'] and report['capture_writer_exited'] and report['ui_timer_stopped'], report
        report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if controller is not None:
            controller.key_repeat.shutdown()
            controller.timer.stop()
            controller.stop.set()
            controller.start_captures.close()
            controller.window.hide()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
