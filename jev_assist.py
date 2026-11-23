"""JEV questions about the game screen beyond OCR tie-breaks.

Each question lists fixed answers; the app keeps its own reading whenever JEV
is unsure. Only reduced copies of game frames are kept, in memory.
"""
import json
import threading
from collections import deque
from pathlib import Path

import jev
import tracker

FRAME_EVERY = 20.0    # seconds between kept frames
FRAME_LIMIT = 60      # 20 minutes of history
FRAME_WIDTH = 960
STALL_SECONDS = 60.0  # EXP unchanged this long while measuring
SCREEN_SECONDS = 15.0  # HUD not found this long before asking what is on screen
BUFF_SECONDS = 120.0  # one JEV look per buff kind in this many seconds


class FrameRing:
    """Reduced copies of recent full game frames, at most one per FRAME_EVERY seconds."""

    def __init__(self):
        self.frames = deque(maxlen=FRAME_LIMIT)
        self.lock = threading.Lock()

    def remember(self, stamp, frame, *, force=False):
        with self.lock:
            if not force and self.frames and stamp - self.frames[-1][0] < FRAME_EVERY:
                return
        scale = min(1.0, FRAME_WIDTH / frame.width)
        small = frame.convert('RGB').resize((round(frame.width*scale), round(frame.height*scale)))
        with self.lock:
            self.frames.append((stamp, small))

    def latest(self, since=float('-inf')):
        with self.lock:
            return self.frames[-1][1] if self.frames and self.frames[-1][0] >= since else None

    def before(self, stamp):
        with self.lock:
            older = [frame for when, frame in self.frames if when <= stamp]
        return older[-1] if older else None

    def clear(self):
        with self.lock:
            self.frames.clear()


def _pick(images, instructions, choices, context):
    value = jev.ask(images, instructions, [(str(i), text) for i, (text, _) in enumerate(choices)],
                    context=context)
    return None if value is None else choices[int(value)][1]


# ---- 2: what changed when hunting efficiency shifted ----

CHANGE_CHOICES = [
    ('The number of party members shown changed', '파티 인원 변화'),
    ('An EXP buff icon appeared or disappeared in the top-right buff bar', '경험치 버프 변화'),
    ('The map (minimap title or surroundings) is different', '사냥터 이동'),
    ('The character is dead or a death/revive dialog is shown in AFTER', '캐릭터 사망'),
    ('AFTER shows a town, shop, NPC dialog or menu instead of hunting', '마을·상점·메뉴'),
]


def ask_change(before, after):
    return _pick([('BEFORE (while EXP per minute was at the old level)', before),
                  ('AFTER (EXP per minute now)', after)],
                 'Hunting EXP per minute changed clearly between BEFORE and AFTER. '
                 'Which visible difference most likely explains it?',
                 CHANGE_CHOICES, 'JEV 효율 변화')


# ---- 3: why EXP stopped ----

STALL_CHOICES = [
    ('The character is dead, or a death or revive dialog is shown', '사망'),
    ('A town, shop, storage or NPC conversation is shown', '마을·상점'),
    ('A loading, channel change or mostly black screen', '로딩·채널 이동'),
    ('A large menu or window covers most of the game', '메뉴·창 열림'),
    ('The character is in a hunting field with no monsters being attacked', '사냥터 대기'),
]


def ask_stall(frame):
    return _pick([('Current game screen', frame)],
                 'This character has gained no EXP for over a minute. What does the screen show?',
                 STALL_CHOICES, 'JEV 멈춘 이유')


def stall_path(base=None):
    return Path(base or tracker.BASE)/'records'/'stall-reasons.json'


def load_stalls(base=None):
    """{session_id: {hunt minute: reason}}"""
    try:
        data = json.loads(stall_path(base).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_stall(session_id, minute, reason, base=None):
    data = load_stalls(base)
    data.setdefault(session_id, {})[str(minute)] = reason
    path = stall_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def stall_text(reasons):
    counts = {}
    for reason in reasons.values():
        counts[reason] = counts.get(reason, 0) + 1
    return ' · '.join(f'{reason} {count}회' for reason, count in counts.items())


# ---- 4: is a registered buff icon shown ----

def ask_buff(icon, corner):
    return _pick([('Registered buff icon', icon), ('Top-right buff bar of the game', corner)],
                 'Is the registered buff icon currently shown in the buff bar? '
                 'A countdown number may cover the bottom of the icon.',
                 [('Yes, the same icon is shown in the buff bar', 'active'),
                  ('No, that icon is not in the buff bar', 'inactive')],
                 'JEV 버프 확인')


# ---- 5: which known character name is shown ----

def ask_nickname(line, names):
    names = list(dict.fromkeys(names))[:200]
    if not names:
        return None
    return _pick([('Character name under the level', line)],
                 'Which character name exactly matches the name in the crop?',
                 [(name, name) for name in names], 'JEV 닉네임 확인')


# ---- 6: what is on screen when the level and EXP display cannot be found ----

SCREEN_CHOICES = [
    ('Gameplay with the bottom status bar showing level and EXP clearly visible',
     '하단 표시는 보여요 · 계속 못 찾으면 수동 영역을 지정해 주세요'),
    ('Gameplay, but the bottom status bar is hidden or covered by a window, chat or menu',
     '하단 레벨·경험치 표시가 가려져 있어요'),
    ('A login, world or character selection screen', '로그인·캐릭터 선택 화면이에요'),
    ('A loading, black or mostly empty screen', '로딩 중이거나 빈 화면이에요'),
    ('Not a game screen (another app or desktop)', '게임 화면이 아니에요 · 다른 창이 잡혔어요'),
]


def ask_screen(frame):
    return _pick([('Captured window', frame)],
                 'This app could not find the level and EXP display. What does the captured window show?',
                 SCREEN_CHOICES, 'JEV 화면 확인')
