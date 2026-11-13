"""Packaged UI and occluded-window OCR verification without changing permissions."""
import json
import os
import platform
import sys
import time
from unittest.mock import patch


def run_diagnostics(application, report_path):
    from PySide6.QtCore import Qt, QPoint, qVersion
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QWidget
    from PIL import Image
    import ctypes
    import objc
    import mss
    import tracker
    from mac_app import Controller, native_overlay
    from macos_support import has_capture_permission
    from window_capture import WindowCapture,pixel_rect

    controller = Controller(application,start_worker=False,restore=False)
    controller.show(); application.processEvents()
    report = {'version':tracker.APP_VERSION,'python':platform.python_version(),
              'machine':platform.machine(),'frozen':bool(getattr(sys,'frozen',False)),
              'ui':'Qt','qt_version':qVersion(),'resource_path':str(tracker.RESOURCES),
              'data_path':str(tracker.BASE),'window_size':[controller.window.width(),controller.window.height()],
              'capture_permission':has_capture_permission()}
    if not report['capture_permission']:
        raise RuntimeError('Screen Recording permission is needed for the window capture test')
    fixture = QWidget(None,Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
    fixture.setWindowTitle('EXP Capture Fixture');fixture.setFixedSize(440,180)
    fixture.setStyleSheet('background:#111111;')
    text = QLabel('395571663[97.48%]',fixture)
    text.setStyleSheet('color:white;background:#111111;font-family:Monaco;font-size:18px;')
    text.setGeometry(40,70,350,40)
    fixture.move(400,300);fixture.show();application.processEvents();QTest.qWait(200)
    view=objc.objc_object(c_void_p=ctypes.c_void_p(int(fixture.winId())))
    window_id=int(view.window().windowNumber())
    def client(): return WindowCapture(bundle_id=None,preferred_id=window_id,expected_pid=os.getpid())
    with patch('window_capture.WindowCapture',side_effect=client), patch.object(controller,'check_permission',return_value=True):
        QTest.mouseClick(controller.region_button,Qt.MouseButton.LeftButton)
        deadline=time.monotonic()+8
        while not controller.overlays and time.monotonic()<deadline:
            application.processEvents(); time.sleep(.02)
        report['region_button_opens_selection']=bool(controller.overlays)
        if not controller.overlays: raise RuntimeError(controller.status.get())
        image_view=controller.overlays[0].image_view
        image=controller.overlays[0].image
        points=image.info['content_points']
        QTest.mousePress(image_view,Qt.MouseButton.LeftButton,pos=QPoint(10,10))
        QTest.mouseRelease(image_view,Qt.MouseButton.LeftButton,pos=QPoint(11,11))
        report['small_selection_shows_feedback']='너무 작아요' in controller.overlays[0].instruction.text()
        report['small_selection_does_not_save']=controller.window_region is None
        sx,sy=image_view.width()/points[0],image_view.height()/points[1]
        start=QPoint(round(40*sx),round(70*sy))
        end=QPoint(round(390*sx)-1,round(110*sy)-1)
        QTest.mousePress(image_view,Qt.MouseButton.LeftButton,pos=start)
        QTest.mouseMove(image_view,end)
        QTest.mouseRelease(image_view,Qt.MouseButton.LeftButton,pos=end)
    application.processEvents()
    report['window_region']=controller.window_region
    report['selection_closes_after_drag']=not controller.overlays
    report['selection_preview_while_stopped']=controller.preview_frame is not None and not controller.running
    with patch.object(controller,'check_permission',return_value=False):
        QTest.mouseClick(controller.toggle_button,Qt.MouseButton.LeftButton)
    report['permission_denial_stays_stopped']=not controller.running
    roi=controller.window_region
    capture=client()
    full=capture.snapshot()
    left,top,width,height=pixel_rect(roi,full.width,full.height)
    expected=full.crop((left,top,left+width,top+height))
    actual=capture.snapshot(roi=roi)
    report['native_crop_matches_selection_pixels']=expected.size==actual.size and expected.tobytes()==actual.tobytes()
    with patch('requests.sessions.Session.request',side_effect=RuntimeError('Offline OCR verification')):
        reader=tracker.LineReader(tracker.create_ocr_engine())
        fixed=Image.open(tracker.RESOURCES/'verification'/'0.4.4_인식복구.png').convert('RGB')
        value,score,raw,_=reader.read(fixed,time.monotonic())
        report['ocr']={'value':value,'confidence':float(score),'raw':raw,'matches_fixture':value==(395571663,97.48),'network_blocked':True}
        new_hud=Image.open(tracker.RESOURCES/'verification'/'new-ui-exp.png').convert('RGB')
        new_reader=tracker.LineReader(reader.engine)
        value,score,raw,_=new_reader.read(new_hud,time.monotonic())
        report['new_ui_ocr']={'value':value,'confidence':float(score),'raw':raw,
                              'variants':new_reader.last_readings,'network_blocked':True}
        if value != (34514347,8.06): raise RuntimeError('New HUD OCR regression')
        if len(new_reader.last_readings) != 2: raise RuntimeError('New HUD fast verification did not agree')
        dotted=Image.open(tracker.RESOURCES/'verification'/'new-ui-label-dot.png').convert('RGB')
        dotted_reader=tracker.LineReader(reader.engine)
        value,score,raw,_=dotted_reader.read(dotted,time.monotonic())
        report['dotted_exp_label']={'value':value,'confidence':float(score),'raw':raw,
                                    'variants':dotted_reader.last_readings,'network_blocked':True}
        if value != (40148011,9.38): raise RuntimeError('Dotted EXP label regression')
        cover=QWidget(None,Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        cover.setStyleSheet('background:#ff00ff;');cover.setGeometry(fixture.geometry())
        cover.show();native_overlay(cover,10);cover.raise_();application.processEvents();QTest.qWait(250)
        def read_window():
            frame=capture.snapshot(roi=roi)
            value,score,raw,_=tracker.LineReader(reader.engine).read(frame,time.monotonic())
            return {'value':value,'confidence':float(score),'raw':raw,'pixels':list(frame.size),'backend':frame.info['capture_backend']}
        report['covered_window_ocr']=read_window()
        with mss.MSS() as desktop:
            screen=tracker.capture_image(desktop,dict(left=fixture.x()+40,top=fixture.y()+70,width=350,height=40))
        report['desktop_sees_occluder']=all(v<20 for v in screen.getextrema()[1])
        text.setText('395571664[97.48%]');application.processEvents();QTest.qWait(250)
        report['covered_window_updated_ocr']=read_window()
        cover.close();fixture.move(650,330);application.processEvents();QTest.qWait(200)
        capture.content_at=0
        report['moved_window_ocr']=read_window()
        # Occlusion was checked above; surface the milestone fixture so Cocoa
        # commits subsequent label paints before taking their immutable frames.
        fixture.raise_();native_overlay(fixture,5);application.processEvents()
        controller.session=tracker.Session(level=169);controller.running=True
        with patch('window_capture.WindowCapture',side_effect=client):
            def observe(expected):
                deadline=time.monotonic()+5
                while True:
                    before=time.monotonic(); wall=time.time()
                    frame=controller.capture_frame({})
                    after=time.monotonic(); stamp=(before+after)/2
                    value,score,raw,_=tracker.LineReader(reader.engine).read(frame,stamp,force=True)
                    if value==expected: break
                    if time.monotonic()>deadline: raise RuntimeError(f'Capture milestone fixture did not update: {value!r} {raw!r}')
                    application.processEvents();QTest.qWait(100)
                details=dict(image=frame,full_game_image=frame.info.get('full_game_image'),
                             capture_started_wall=wall,capture_finished_wall=wall+after-before,
                             capture_started_monotonic=before,capture_finished_monotonic=after,
                             backend=frame.info['capture_backend'],window_region=frame.info['window_region'],
                             window_id=frame.info['window_id'])
                controller.events.put((controller.generation,value,raw,score,None,stamp,details))
                controller.poll()
                return frame
            observe((395571664,97.48))
            QTest.qWait(550)
            observe((395571664,97.48))
            text.setText('395571764[97.48%]');application.processEvents();QTest.qWait(250)
            hunting_frame=observe((395571764,97.48))
            controller.start_captures.flush()
            directory=tracker.BASE/'records'/'start-captures'/controller.session.session_id
            metadata=json.loads((directory/'hunt-start.json').read_text())
            saved=Image.open(directory/'hunt-start.png').convert('RGB')
            x,y,w,h=pixel_rect(roi,saved.width,saved.height)
            report['start_captures']={
                'stages':sorted(p.stem for p in directory.glob('*.json')),
                'same_frame_full_and_roi':saved.crop((x,y,x+w,y+h)).tobytes()==hunting_frame.tobytes(),
                'full_window_pixels':list(saved.size), 'gained':metadata['gained'],
                'baseline':metadata['baseline'], 'current':metadata['current'],
                'returns_to_fast_roi':controller.capture_frame({}).info.get('full_game_image') is None}
        fixture.close()
    controller.session=tracker.Session(level=169);controller.running=True
    for stamp,reading in ((0,(172680132,42.55)),(1,(172680132,42.55)),(3,(172680232,42.55)),(63,(172683232,42.55))):
        controller.events.put((controller.generation,reading,str(reading),.99,None,stamp,{}));controller.poll()
    report['ledger_gain']=controller.session.stats(63)[0]
    report['gain_label']=controller.total_label.text()
    report['saved_csv']=bool(list((tracker.BASE/'records').glob('*.csv')))
    controller.shutdown()
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert report['region_button_opens_selection'],report
    assert report['small_selection_shows_feedback'] and report['small_selection_does_not_save'],report
    assert report['selection_closes_after_drag'],report
    assert report['selection_preview_while_stopped'],report
    assert report['native_crop_matches_selection_pixels'],report
    assert report['permission_denial_stays_stopped'],report
    assert report['ocr']['matches_fixture'],report
    assert report['desktop_sees_occluder'],report
    assert report['covered_window_ocr']['value']==(395571663,97.48),report
    assert report['covered_window_updated_ocr']['value']==(395571664,97.48),report
    assert report['moved_window_ocr']['value']==(395571664,97.48),report
    assert report['ledger_gain']==3100 and report['gain_label']=='3,100' and report['saved_csv'],report
    milestones=report['start_captures']
    assert milestones['stages']==['baseline','first-frame','hunt-start'],report
    assert milestones['same_frame_full_and_roi'] and milestones['gained']==100 and milestones['returns_to_fast_roi'],report
