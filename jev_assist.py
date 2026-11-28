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
MESO_MISSING_SECONDS = 6.0   # meso box not found this long before asking where the inventory is
MESO_CLOSED_SECONDS = 600.0  # after JEV saw no inventory (or was unsure), wait this long before asking again
SCREEN_REPEAT_SECONDS = 300.0  # ask again about the same missing HUD at most this often


class FrameRing:
    """JPEG-compressed copies of recent full game frames, at most one per FRAME_EVERY seconds.

    Storing compressed JPEG bytes instead of uncompressed RGB bitmaps cuts ring memory
    from ~90 MB down to ~2.5 MB across 60 frames while decoding on demand when JEV asks.
    """

    def __init__(self):
        self.frames = deque(maxlen=FRAME_LIMIT)
        self.lock = threading.Lock()

    @staticmethod
    def _encode(frame):
        import io
        scale = min(1.0, FRAME_WIDTH / frame.width)
        small = frame if frame.mode == 'RGB' else frame.convert('RGB')
        if scale < 1.0:
            small = small.resize((round(frame.width * scale), round(frame.height * scale)))
        buf = io.BytesIO()
        small.save(buf, format='JPEG', quality=80)
        return buf.getvalue()

    @staticmethod
    def _decode(data):
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            return img.convert('RGB')

    def remember(self, stamp, frame, *, force=False):
        with self.lock:
            if not force and self.frames and stamp - self.frames[-1][0] < FRAME_EVERY:
                return
        encoded = self._encode(frame)
        with self.lock:
            self.frames.append((stamp, encoded))

    def latest(self, since=float('-inf')):
        with self.lock:
            data = self.frames[-1][1] if self.frames and self.frames[-1][0] >= since else None
        return self._decode(data) if data is not None else None

    def before(self, stamp):
        with self.lock:
            older = [data for when, data in self.frames if when <= stamp]
        return self._decode(older[-1]) if older else None

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
    import records_backup
    path = stall_path(base)
    with records_backup.SHARED_JSON_LOCK:
        data = load_stalls(base)
        data.setdefault(session_id, {})[str(minute)] = reason
        records_backup.atomic_write_json(path, data)


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


def ask_buffs(icons_by_kind, corner):
    """Check multiple registered buff icons against the same buff bar in one JEV request."""
    if len(icons_by_kind) == 1:
        kind, icon = next(iter(icons_by_kind.items()))
        return {kind: ask_buff(icon, corner)}
    images = [('Top-right buff bar of the game', corner)]
    items = []
    for kind, icon in icons_by_kind.items():
        images.append((f'Registered buff icon ({kind})', icon))
        items.append((
            kind,
            f'Is the registered buff icon ({kind}) currently shown in the top-right buff bar? '
            'A countdown number may cover the bottom of the icon.',
            [('active', 'Yes, that icon is shown in the buff bar'),
             ('inactive', 'No, that icon is not in the buff bar')],
        ))
    return jev.ask_many(images, items, context='JEV 버프 확인')


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


# ---- where the item inventory is, when the meso box cannot be found locally ----

QUADRANTS = {
    'tl': ('Open, with the meso amount in the top-left quarter of the screen', (0.0, 0.0, .6, .6)),
    'tr': ('Open, with the meso amount in the top-right quarter of the screen', (.4, 0.0, 1.0, .6)),
    'bl': ('Open, with the meso amount in the bottom-left quarter of the screen', (0.0, .4, .6, 1.0)),
    'br': ('Open, with the meso amount in the bottom-right quarter of the screen', (.4, .4, 1.0, 1.0)),
}
HINT_PROBABILITY = .6   # a wrong hint only narrows a second search, so a lower bar is safe
CLOSED_PROBABILITY = jev.MIN_PROBABILITY  # "closed" is shown to the user, so it keeps the usual bar


def ask_inventory(frame):
    """A QUADRANTS key for where the inventory meso amount is, 'closed', or None.

    In tests on real screens JEV pointed at the right quarter at 41-79%, below the
    usual bar, so a quarter is accepted from HINT_PROBABILITY.
    """
    choices = [(key, text) for key, (text, _) in QUADRANTS.items()]
    choices.append(('closed', 'No item inventory window is open'))
    value, probability = jev.ask(
        [('Game screen', frame)],
        'Is the ITEM INVENTORY window (item grid with a meso amount at its bottom) open on screen? '
        'Where is its meso amount?',
        choices, context='JEV 인벤토리 위치', minimum=HINT_PROBABILITY, scored=True)
    if value == 'closed' and probability < CLOSED_PROBABILITY:
        return None
    return value


def quadrant_region(key):
    """Normalized (left, top, right, bottom) of a quarter, overlapping the middle of the screen."""
    return QUADRANTS[key][1]


# ---- large meso change cause: shop/trade/storage vs hunting/NPC loot sale ----

MESO_CHANGE_CHOICES = [
    ('A player trade window, storage, auction/free market, or buying potions/items from a shop (meso spent or transferred)', '거래·구매·창고'),
    ('Normal monster hunting in a field with no shop, trade or storage window open', '사냥 획득'),
    ('Selling hunt drops/junk/equipment to an NPC merchant (meso gained from selling loot)', '사냥 획득'),
]


def ask_meso_change(frame, delta):
    """Ask JEV whether a large meso jump is trade/purchase/storage or hunting profit (incl. NPC loot sale)."""
    direction = f'+{delta:,}' if delta >= 0 else f'{delta:,}'
    return _pick(
        [('Game screen near the meso change', frame)],
        f'The player meso balance changed suddenly by {direction}. '
        'Is this a player trade, storage transfer, or buying items/potions, '
        'or normal hunting / selling hunt loot to an NPC?',
        MESO_CHANGE_CHOICES,
        'JEV 큰 메소 변동',
    )


# ---- seamless potion preset recognition when registering a quickslot icon ----

POTION_PRESETS = [
    ('Red Potion (small red bottle, beginner HP potion)', '빨간 포션', 50),
    ('Orange Potion (small orange bottle, HP potion)', '주황 포션', 160),
    ('White Potion (small white/silver bottle, HP potion)', '하얀 포션', 320),
    ('Blue Potion (small blue bottle, MP potion)', '파란 포션', 200),
    ('Mana Elixir (tall cyan/blue glowing bottle, MP)', '마나 엘릭서', 620),
    ('Unagi / Roasted Eel on a plate (HP food)', '장어구이', 1100),
    ('Pure Water / Clear Water glass bottle (sky-blue MP bottle)', '맑은 물', 1650),
    ('Jujuba / Icebar popsicle on stick (yellow-orange HP)', '쭈쭈바', 2300),
    ('Patbingsu / Shaved Ice bowl with red bean (MP)', '팥빙수', 4000),
    ('Reindeer Milk bottle (white milk bottle, HP)', '순록의 우유', 5600),
    ('Sunset Dew / Twilight Dew bottle (orange-pink MP bottle)', '황혼의 이슬', 10200),
    ('Elixir (ornate blue/purple potion, 50% HP/MP)', '엘릭서', 0),
    ('Power Elixir (ornate golden/blue potion, 100% HP/MP)', '파워 엘릭서', 0),
    ('All Cure Potion (green/white medicine bottle)', '만병통치약', 400),
    ('Pain Reliever pill/bottle (defense medicine)', '통증 완화제', 1200),
]


def ask_potion_preset(icon, *, timeout=5):
    """Quietly identify a common MapleStory potion icon to prefill name & NPC shop price, or None."""
    choices = [(str(i), desc) for i, (desc, _, _) in enumerate(POTION_PRESETS)]
    try:
        picked = jev.ask(
            [('Quickslot consumable icon', icon)],
            'Which MapleStory consumable item matches this quickslot icon? '
            'Ignore any hotkey letter at the top-left and quantity digits at the bottom.',
            choices, context='JEV 포션 인식', minimum=0.80, timeout=timeout,
        )
    except jev.Unavailable:
        return None
    if picked is None:
        return None
    _, name, cost = POTION_PRESETS[int(picked)]
    return name, cost
