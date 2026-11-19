"""Rebuild with the local environment pinned in requirements-macos.lock."""
from pathlib import Path
import re
import subprocess
import sys
from PIL import Image

# Self-signed identity kept in this Mac's login keychain. A fixed certificate keeps
# macOS privacy permissions across updates; ad-hoc signatures change every build.
SIGN_IDENTITY = 'Planet Helper Signing'

BASE = Path(__file__).resolve().parent
subprocess.run(['/bin/zsh', str(BASE/'native'/'build.sh')], check=True)
with Image.open(BASE/'planet-helper-icon.png') as icon:
    icon.save(BASE/'PlanetEXP.icns', sizes=[(16, 16), (32, 32), (64, 64),
                                            (128, 128), (256, 256), (512, 512), (1024, 1024)])
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                '--distpath', str(BASE.parent/'dist'), '--workpath', str(BASE.parent/'build'),
                str(BASE/'PlanetEXP.spec')], cwd=BASE, check=True)
version = re.search(r"^APP_VERSION = '([0-9.]+)'$", (BASE/'tracker.py').read_text(), re.M)[1]
app = BASE.parent/'dist'/f'플래닛 도우미 {version}.app'
identities = subprocess.run(['/usr/bin/security', 'find-identity', '-p', 'codesigning'],
                            capture_output=True, text=True, check=True).stdout
if f'"{SIGN_IDENTITY}"' in identities:
    subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', SIGN_IDENTITY, str(app)], check=True)
else:
    print(f'경고: "{SIGN_IDENTITY}" 인증서가 없어 임시 서명으로 남깁니다. 업데이트 후 권한을 다시 물을 수 있습니다.',
          file=sys.stderr)
