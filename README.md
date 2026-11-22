# 플래닛 도우미 (Planet Helper)

[![tests](https://github.com/YSoung99/planet-helper/actions/workflows/tests.yml/badge.svg)](https://github.com/YSoung99/planet-helper/actions/workflows/tests.yml)

MapleStory Worlds의 메이플 플래닛에서 **경험치 획득량을 화면 판독(OCR)으로 측정**하는 데스크톱 도우미예요.
게임 메모리를 읽거나 게임 파일을 건드리지 않고, 화면 하단의 `EXP 숫자 [퍼센트]`만 읽어요.

- 총 획득 경험치, 누적·최근 3분 분당 평균, 최근 1분 / 최근 6분 획득(사냥 시간 기준 · 10초마다 갱신 · 6분 전에는 예상치 · 누적 평균 대비 ▲▼%)
- 시간당 경험치와 레벨업까지 남은 시간
- 레벨업해도 측정이 끊기지 않고 이어짐
- 자동 측정(오른쪽 클릭 → **경험치 증가 시 자동 측정**, 기본 꺼짐): 대기 중 1분 안에 경험치가 두 번 오르면 알아서 시작하고, 5분 동안 멈추면 알아서 종료·저장. 직접 정지하면 경험치가 1분 멈춘 뒤부터 다시 자동 시작
- 휴식(경험치 3분 이상 정지, 마지막 획득 직후부터) 시간 자동 제외 → 순수 사냥 시간 기준 평균·1분·6분
- 사냥 기록 비교 표·효율 그래프 + 사냥터별 평균
- 측정 시작 시 미니맵의 맵 이름을 읽어 사냥터를 자동 태그 (한 번 고치면 같은 맵은 계속 고친 이름으로)
- 메소 기록: 인벤토리 창을 열면 아래 메소 칸을 읽어 `records/meso.csv`에 남기고, 측정 시작·끝 메소 차이를 사냥 기록의 **메소** 칸에 붙여요. 창을 옮겨도 다시 찾아요.
  - OCR이 한두 글자 틀리면(예: `낮은 성벽스`) 미니맵 제목 부분만 LLM Gateway의 JEV에 보내, 전에 고쳐 둔 이름 중 맞는 것을 고르게 해요. 처음 보는 맵은 직접 한 번 고쳐 줘야 해요.
  - 키: 앱에서 오른쪽 클릭 → **JEV API 키 설정**으로 입력해요. 맥 키체인 / 리눅스 키링(`local.dotmaker.llmgateway`, 도트메이커와 공유)에만 저장되고 파일·GitHub에는 남지 않아요. 환경변수 `LLM_GATEWAY_API_KEY`가 있으면 그걸 먼저 써요. 키가 없거나 인터넷이 끊기면 그냥 OCR 결과를 써요.
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

앱 메뉴에서 **플래닛 도우미**를 실행하세요. 기록과 설정은 최초 설치 폴더의 `records/`, `settings.json`에 저장돼요. 기존 설치라면 위 설치 스크립트를 새 소스에서 한 번 다시 실행해야 앱 내 업데이트가 연결돼요.

릴리스에서 `planet-helper-linux.tar.gz`를 내려받아 빈 폴더에 풀고 `./install-linux.sh`를 실행해도 돼요. 가상환경과 고정 의존성을 설치하려면 Python 3.14, venv, 인터넷 연결이 필요해요.

선택: Rust OCR 가속 모듈은 `native/ocr-rust/README.md`를 따라 빌드하면 `_planet_ocr.so`로 사용돼요. 없으면 기본 RapidOCR을 써요.

## macOS 설치 (Apple Silicon, macOS 14 이상)

[GitHub 릴리스](https://github.com/YSoung99/planet-helper/releases)에서 `planet-helper-macos-arm64.zip`을 내려받아 압축을 풀고 앱을 응용 프로그램에 옮겨요. 소스에서 실행하거나 빌드할 수도 있어요.

```bash
git clone https://github.com/YSoung99/planet-helper.git ~/planet-helper
cd ~/planet-helper
python3 -m venv .venv && .venv/bin/pip install -r requirements-macos.lock
.venv/bin/python mac_app.py            # 바로 실행
.venv/bin/python build_macos.py        # 또는 .app 번들 만들기 (Xcode 명령줄 도구 필요)
```

처음 실행하면 **화면 기록** 권한(연타를 쓰면 **손쉬운 사용·입력 모니터링**도)을 허용해야 해요.
기록은 `~/Library/Application Support/Maple Planet Helper Auto Trial/`에 저장돼요.

## 업데이트와 사냥 기록

**경험치 버프 감지 (시험 기능)** · 설정에서 **경험치 버프 아이콘 등록**을 열고 게임 우상단에서 경쿠 또는 홀리 심볼 아이콘 한 칸을 잘라 등록하세요. 측정 중 연속 캡처에서 같은 아이콘을 확인하면 **사용 중**을 표시합니다. 감지가 어려운 화면은 **확인 불가**로 표시하며, 확인된 구간은 사냥 기록 비교에서 기록 줄에 마우스를 올리면 볼 수 있어요. 등록 이미지와 감지 작업은 기기 안에 남습니다.

설치된 앱에서 **업데이트**를 누르면 새 릴리스를 확인하거나 이전 설치로 롤백할 수 있어요. 앱은 시작할 때와 실행 중 30분마다 설치 가능한 릴리스를 확인해요. 업데이트와 롤백 직전에는 진행 중인 측정을 저장하고 앱을 다시 열어요.

**사냥 기록 비교**에서 닉네임으로 기록과 효율을 볼 수 있어요. 닉네임이 없는 예전 기록은 **캐릭터 미확인**에 표시돼요.
창 맨 위에는 오늘·이번 주 요약 카드가 있고, 아래 **기록 · 사냥터별 · 레벨별** 탭으로 나눠 봐요. 휴식, 인식 공백, 시간당, 효율 변화, 경험치 버프는 기록 줄에 마우스를 올리면 보여요.
캐릭터와 사냥터 메모는 두 번 눌러 고치고, 여러 줄을 고른 뒤 오른쪽 클릭하면 한꺼번에 바꿀 수 있어요. 고친 이름은 `records/character-names.json`에 따로 저장되고 원본 기록은 그대로예요.
띄어쓰기만 다른 사냥터 이름(`사자왕의성`/`사자왕의 성`)은 사냥터별 평균에서 하나로 묶고, 새로 태그할 때도 이미 쓰던 표기를 따라가요.

오른쪽 클릭 → **현재 캐릭터**를 정해 두면 새 기록에 그 이름이 붙어요. 화면에서 읽은 닉네임이나 기록에서 직접 고친 이름이 있으면 그쪽이 먼저예요.
오른쪽 클릭 → **오늘·이번 주 요약**(사냥 기록 비교 창 맨 위에도 표시)에서 횟수, 순수 사냥 시간, 획득량, 분당 평균, 레벨업, 최고 효율 사냥터를 볼 수 있어요.

**메소 기록** · 인벤토리 창(`ITEM INVENTORY`)을 열면 맨 아래 `메소` 칸을 2초마다 읽어요. 같은 숫자가 두 번 연속 읽혀야 기록하고, 한 번에 10배 넘게 바뀐 숫자는 한 번 더 확인해요. 인벤토리를 닫아 두면 읽지 않아요.
측정 시작 후 30초 안에 인벤토리를 열어 첫 잔액을 확인하고, 종료 직전에 다시 열어 마지막 잔액이 표시되면 측정을 정지하세요. 종료 전 12초 안의 확인값을 사용해요. 두 잔액을 확보하면 기록 표의 **메소** 칸에 차이가 표시돼요.
잔액 차이가 100만을 넘으면 **큰 변동**으로 표시해요. 오른쪽 클릭 → **메소 큰 변동 기준 금액**에서 기준을 바꿀 수 있어요. 거래·구매가 맞다면 측정 중 **이번 측정의 큰 메소 변동 분류**에서 해당 변동을 골라 측정 결과에서 제외할 수 있어요.
잔액이 바뀌면 `records/meso.csv`에 시각·캐릭터·메소로 남아요. 상세 화면의 **시작 잔액**과 **종료 잔액** 상태로 정지 전에 기록 조건을 확인할 수 있어요.

**접속 닉네임**은 자동 HUD 인식이 동작할 때 상세 화면에 표시돼요. 판독이 어려우면 오른쪽 클릭 → **현재 닉네임 등록·수정**에서 화면에 보이는 이름을 등록하세요. 게임 UI나 배율이 바뀌면 다시 등록이 필요할 수 있어요.

## 기록 백업과 구글 드라이브 동기화

- 앱을 켤 때 하루 한 번 `records-backups/`에 기록을 백업하고 14일치를 보관해요. 진단 로그와 캡처 이미지는 빼요.
- 오른쪽 클릭 → **기록 백업·옮기기**에서 백업 파일(zip)로 내보내거나, 다른 컴퓨터의 백업을 가져와 합칠 수 있어요. 가져오기는 없는 기록과 이름만 더하고, 이미 있는 것은 바꾸지 않아요.
- **구글 드라이브 동기화**: [rclone](https://rclone.org)을 설치하고 한 번 연결한 뒤(`rclone config create gdrive drive scope=drive.file`), 같은 메뉴의 **구글 드라이브 동기화 설정…**에서 연결을 고르세요. 드라이브의 `planet-helper/`에 컴퓨터마다 `records-<컴퓨터>.zip`을 올리고, 앱을 켤 때 다른 컴퓨터의 파일을 받아 합쳐요. 측정이 끝날 때마다 이 컴퓨터의 백업을 다시 올려요. `drive.file` 권한이라 rclone이 올린 파일만 볼 수 있어요.

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
