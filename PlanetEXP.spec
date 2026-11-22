from pathlib import Path
import re
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

source = Path(SPECPATH)
version = re.search(r"^APP_VERSION = '([0-9.]+)'$", (source/'tracker.py').read_text(), re.M)[1]
data = collect_data_files('rapidocr', excludes=['models/**'])
data += [(str(source/'exp_table.json'), '.'), (str(source/'models'), 'models'), (str(source/'mac_update_swap.sh'), '.')]
# Game screenshots used by self-checks are kept out of the public repository;
# bundle them only when they exist on this machine.
data += [(str(path), 'verification') for path in sorted((source/'verification').glob('*.png'))]
a = Analysis([str(source/'mac_app.py')], pathex=[str(source)], binaries=[(str(source/'native'/'KeyRepeatHelper'), 'native')], datas=data,
             hiddenimports=collect_submodules('rapidocr') + ['AppKit', 'Foundation', 'Quartz', 'ScreenCaptureKit', 'mss.darwin'],
             excludes=['tkinter', '_tkinter', 'diagnostics', 'pytest', 'matplotlib', 'torch', 'paddle', 'openvino', 'tensorflow', 'IPython'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PlanetHelper',
          console=False, debug=False, strip=False, upx=False, target_arch='arm64',
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='PlanetHelper')
app = BUNDLE(coll, name=f'플래닛 도우미 {version}.app', icon=str(source/'PlanetEXP.icns'),
             bundle_identifier='local.codex.planethelper.autotrial',
             info_plist={'CFBundleDisplayName': f'플래닛 도우미 {version}', 'CFBundleName': f'플래닛 도우미 {version}',
                         'CFBundleShortVersionString': version, 'CFBundleVersion': '26',
                         'NSHighResolutionCapable': True,
                         'LSMinimumSystemVersion': '14.0',
                         'NSScreenCaptureUsageDescription': '게임 화면의 경험치 숫자를 읽어 획득량을 계산합니다.',
                         'NSInputMonitoringUsageDescription': '게임에서 A와 Enter를 누르는 동안 반복 입력을 제공합니다.',
                         'NSAccessibilityUsageDescription': '게임에서 A와 Enter의 반복 입력을 전달합니다.'})
