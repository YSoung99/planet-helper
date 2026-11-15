"""Rebuild with the local environment pinned in requirements-macos.lock."""
from pathlib import Path
import subprocess
import sys
from PIL import Image

BASE = Path(__file__).resolve().parent
subprocess.run(['/bin/zsh', str(BASE/'native'/'build.sh')], check=True)
with Image.open(BASE/'planet-helper-icon.png') as icon:
    icon.save(BASE/'PlanetEXP.icns', sizes=[(16, 16), (32, 32), (64, 64),
                                            (128, 128), (256, 256), (512, 512), (1024, 1024)])
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                '--distpath', str(BASE.parent/'dist'), '--workpath', str(BASE.parent/'build'),
                str(BASE/'PlanetEXP.spec')], cwd=BASE, check=True)
