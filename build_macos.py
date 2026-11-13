"""Rebuild with the local environment pinned in requirements-macos.lock."""
from pathlib import Path
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
subprocess.run(['/bin/zsh', str(BASE/'native'/'build.sh')], check=True)
image = Image.new('RGBA', (1024, 1024))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((36, 36, 988, 988), radius=210, fill='#151b24')
draw.rounded_rectangle((145, 195, 879, 655), radius=70, fill='#253a30', outline='#a8edbb', width=14)
font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial Bold.ttf', 208)
draw.text((512, 416), 'EXP', font=font, fill='#e7fff0', anchor='mm')
draw.rounded_rectangle((156, 738, 868, 804), radius=28, fill='#34424b')
draw.rounded_rectangle((156, 738, 690, 804), radius=28, fill='#a8edbb')
image.save(BASE/'PlanetEXP.icns', sizes=[(128, 128), (256, 256), (512, 512), (1024, 1024)])
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                '--distpath', str(BASE.parent/'dist'), '--workpath', str(BASE.parent/'build'),
                str(BASE/'PlanetEXP.spec')], cwd=BASE, check=True)
