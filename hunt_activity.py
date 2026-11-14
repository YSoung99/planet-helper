"""Read visual evidence of hunting independently of the EXP accounting."""
from dataclasses import dataclass
import re


SCAN_INTERVAL = 2.0
OBSERVATION_LIMIT = 8.0
REWARD = re.compile(r'(경험치|메소)를얻었습니다\(?\+([\d,]+)')


def reward_types(lines):
    found = set()
    for text, score in lines:
        if score < .80:
            continue
        match = REWARD.search(re.sub(r'\s+', '', text))
        if match and match[2].replace(',', '').isdigit():
            found.add(match[1])
    return frozenset(found)


@dataclass(frozen=True)
class Observation:
    rewards: frozenset
    interrupted: bool = False


def read(frame):
    import numpy as np
    import map_name

    width, height = frame.size
    header = np.asarray(frame.crop((round(width*.43), round(height*.208),
                                    round(width*.57), round(height*.253))).convert('RGB'))
    # The dialog has a flat dark header with light text. Most frames can skip OCR here.
    edges = header[:, [0, -1], :]
    if edges.std() < 6 and 32 <= edges.mean() <= 64:
        from PIL import Image
        title = Image.fromarray(header).resize((272, 50))
        for text, score in map_name.recognize_lines(title, detect=False):
            if score >= .85 and '투명도형찾기' in re.sub(r'\s+', '', text):
                return Observation(frozenset(), interrupted=True)

    crop = frame.crop((round(width*.715), round(height*.67), width, round(height*.88)))
    return Observation(reward_types(map_name.recognize_lines(crop)))


@dataclass
class Activity:
    generation: int = -1
    observed: float = float('-inf')
    gained: float = float('-inf')
    observation: Observation | None = None

    def update(self, generation, stamp, observation=None, *, gained=False):
        if generation != self.generation:
            self.generation = generation
            self.gained = self.observed = float('-inf')
            self.observation = None
        if observation is not None:
            self.observed, self.observation = stamp, observation
            if observation.interrupted and self.gained <= stamp:
                self.gained = float('-inf')
            return
        if gained:
            self.gained = stamp

    def text(self, generation, now):
        if self.generation != generation or now - self.observed > OBSERVATION_LIMIT:
            return '사냥 신호 확인 중'
        if self.observation.interrupted:
            return '사냥 중단 · 게임 확인창'
        if now - self.gained <= OBSERVATION_LIMIT:
            return ('사냥 중 · 경험치 문구와 증가 확인' if '경험치' in self.observation.rewards
                    else '사냥 중 · 경험치 증가')
        if '경험치' in self.observation.rewards:
            return '경험치 획득 문구 · 증가 확인 대기'
        if '메소' in self.observation.rewards:
            return '메소 획득 문구 · 사냥 여부 확인 중'
        return '획득 대기 · 사냥 여부 확인 중'
