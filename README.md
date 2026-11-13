# 플래닛 도우미 (Planet Helper)

[![tests](https://github.com/YSoung99/planet-helper/actions/workflows/tests.yml/badge.svg)](https://github.com/YSoung99/planet-helper/actions/workflows/tests.yml)

MapleStory Worlds의 메이플 플래닛에서 **경험치 획득량을 화면 판독(OCR)으로 측정**하는 데스크톱 도우미예요.
게임 메모리를 읽거나 게임 파일을 건드리지 않고, 화면 하단의 `EXP 숫자 [퍼센트]`만 읽어요.

- 총 획득 경험치, 누적·최근 3분 분당 평균, 직전 1분 / 최근 6분 획득(6분 전에는 예상치)
- 시간당 경험치와 레벨업까지 남은 시간
- 레벨업해도 측정이 끊기지 않고 이어짐
- 휴식(경험치 1분 이상 정지) 시간 자동 제외 → 순수 사냥 시간 기준 평균
- 사냥 기록 비교 표·효율 그래프 + 사냥터별 평균
- 측정 시작 시 미니맵의 맵 이름을 읽어 사냥터를 자동 태그 (한 번 고치면 같은 맵은 계속 고친 이름으로)
- 사냥 비율 표시, 측정 중 효율이 크게 바뀌면 "뭐가 달라졌나요?" 태그 요청
- Linux(KDE)와 macOS(Apple Silicon) 지원

## 구조: 한 곳을 고치면 두 플랫폼에 같이 반영

| 파일 | 역할 |
|---|---|
| `qt_controller.py` | **공통 화면·측정 로직** (Linux·Mac 모두 사용) |
| `tracker.py`, `rates.py`, `record_history.py`, `display_panel.py`, `auto_hud.py`, `temporal_ocr.py` … | 공통 계산·OCR·UI |
| `linux_app.py`, `linux_capture.py`, `linux_key_repeat.py` | Linux 전용 (창 캡처, 맨 위 고정, 키 입력) |
| `mac_app.py`, `window_capture.py`, `key_repeat.py`, `native/*.swift` | macOS 전용 (ScreenCaptureKit, 권한, 키 입력) |

새 기능은 공통 파일에 넣으면 두 버전에 같이 들어가요. 플랫폼별 차이는 `qt_controller.SharedController`의
"Platform hook" 메서드만 각 `*_app.py`에서 덮어써요.

## Linux 설치

```bash
git clone https://github.com/YSoung99/planet-helper.git ~/.local/share/planet-helper
cd ~/.local/share/planet-helper && ./install-linux.sh
```

앱 메뉴에서 **플래닛 도우미**를 실행하세요. 기록과 설정은 같은 폴더의 `records/`, `settings.json`에 저장되며 git에는 올라가지 않아요.

선택: Rust OCR 가속 모듈은 `native/ocr-rust/README.md`를 따라 빌드하면 `_planet_ocr.so`로 사용돼요. 없으면 기본 RapidOCR을 써요.

## macOS 설치 (Apple Silicon, macOS 14 이상)

```bash
git clone https://github.com/YSoung99/planet-helper.git ~/planet-helper
cd ~/planet-helper
python3 -m venv .venv && .venv/bin/pip install -r requirements-macos.lock
.venv/bin/python mac_app.py            # 바로 실행
.venv/bin/python build_macos.py        # 또는 .app 번들 만들기 (Xcode 명령줄 도구 필요)
```

처음 실행하면 **화면 기록** 권한(연타를 쓰면 **손쉬운 사용·입력 모니터링**도)을 허용해야 해요.
기록은 `~/Library/Application Support/Maple Planet Helper Auto Trial/`에 저장돼요.

## 두 컴퓨터 동기화

```bash
git pull                 # 다른 컴퓨터에서 고친 내용 받기
git add -A && git commit -m "변경 내용" && git push   # 이 컴퓨터에서 고친 내용 올리기
```

`.gitignore`는 허용 목록 방식이라, 사냥 기록·설정·로그·스크린샷은 `git add -A`를 해도 올라가지 않아요.

## 테스트

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
```

`git push`할 때마다 GitHub Actions가 Linux와 macOS에서 같은 테스트를 자동으로 돌려요.

## 라이선스와 출처

- OCR: [RapidOCR](https://github.com/RapidAI/RapidOCR) (Apache-2.0, `references/licenses/`), PaddleOCR PP-OCR 모델(Apache-2.0)
- 경험치표: 커뮤니티 참고 자료 기반 (`경험치표_근거.md`)
- 비공식 팬 도구이며 NEXON / MapleStory Worlds와 관련이 없어요. 사용에 따른 책임은 사용자에게 있어요.
