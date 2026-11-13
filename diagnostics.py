"""Local packaging verification; never changes macOS permissions."""
import json
import platform
import sys
import time


def run_diagnostics(report_path):
    import tkinter as tk
    import mss
    from PIL import Image
    import tracker
    from macos_support import has_capture_permission, configure_window
    report = {'version': tracker.APP_VERSION, 'machine': platform.machine(),
              'python': platform.python_version(), 'frozen': bool(getattr(sys, 'frozen', False)),
              'resource_path': str(tracker.RESOURCES), 'data_path': str(tracker.BASE)}
    root = tk.Tk()
    root.title('Planet EXP Diagnostics')
    root.geometry('280x492+30+90')
    root.overrideredirect(True)
    root.update()
    report['tk_version'] = str(root.tk.call('info', 'patchlevel'))
    report['cocoa_window'] = configure_window(root, (30, 90, 280, 492))
    root.update()
    report['content_size'] = [root.winfo_width(), root.winfo_height()]
    root.withdraw()
    root.update()
    with mss.MSS() as capture:
        report['monitors'] = [dict(m) for m in capture.monitors]
        report['capture_permission'] = has_capture_permission()
        if report['capture_permission']:
            m = capture.monitors[1]
            region = {'left': m['left']+30, 'top': m['top']+90, 'width': 200, 'height': 60}
            frame = tracker.capture_image(capture, region)
            report['capture'] = {'region_points': region, 'image_pixels': list(frame.size),
                                 'extrema': frame.getextrema(), 'backend': frame.info['capture_backend']}
    start = time.monotonic()
    reader = tracker.LineReader(tracker.create_ocr_engine())
    im = Image.open(tracker.RESOURCES/'verification'/'0.4.4_인식복구.png').convert('RGB')
    value, confidence, raw, cached = reader.read(im, time.monotonic())
    report['ocr'] = {'value': value, 'confidence': float(confidence), 'raw': raw,
                     'variants': reader.last_readings, 'seconds': time.monotonic()-start,
                     'matches_fixture': value == (395571663, 97.48)}
    root.destroy()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if (not report['cocoa_window'] or report['content_size'] != [280, 492]
            or not report['ocr']['matches_fixture']):
        raise RuntimeError('Packaging verification failed: '+str(report_path))
