# 플래닛 도우미 Rust OCR

Linux용 플래닛 도우미 1.4.0-auto.3에서 경험치와 레벨의 문자 판독을 교체하는 모듈이와요. 기존 앱이 사용하는 Python과 ONNX Runtime을 그대로 이용한답니다. 게임 창 캡처와 경험치 줄 위치 탐색은 기존 앱이 담당한답니다.

레벨이 확인되면 영어·숫자용 PP-OCRv4 경량 모델이 먼저 읽는답니다. 애매한 화면은 PP-OCRv6 중형 모델로 재판독하며, 숫자와 퍼센트의 일치 여부를 검증한답니다. 기존의 연속 프레임 확인도 적용된답니다. [PaddleOCR 모델 문서](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/module_usage/text_recognition.html)

## 빌드

플래닛 도우미가 설치된 Linux 환경에서 아래 명령을 실행하시와요. Cargo로 릴리스 빌드를 하고 앱의 Python 버전에 맞는 모듈을 만든답니다. [PyO3 빌드 문서](https://pyo3.rs/main/building-and-distribution.html)

```sh
app="$HOME/.local/share/planet-helper"
PYO3_PYTHON="$app/.venv/bin/python" cargo build --release --locked
PYO3_PYTHON="$app/.venv/bin/python" cargo test --release --locked

model=en_PP-OCRv4_rec_mobile.onnx
curl -fL "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv4/rec/$model" -o "$model"
printf '%s  %s\n' e8770c967605983d1570cdf5352041dfb68fa0c21664f49f47b155abd3e0e318 "$model" | sha256sum -c
```

## 앱에 연결

패치는 원본 1.4.0-auto.3 기준이와요. 이미 적용한 설치본에서 엔진만 다시 빌드하는 경우에는 패치와 Python 파일 복사를 생략하시와요. 앱을 새로 실행하면 모듈을 불러오고, 진단 기록의 `ocr_engine` 필드에 `rust`가 기록된답니다.

```sh
patch_file="$PWD/integration/planet-helper-1.4.0-auto.3.patch"
patch --dry-run --batch --forward -d "$app" -p1 < "$patch_file"
patch --batch --forward -d "$app" -p1 < "$patch_file"
install -m 0644 integration/native_ocr.py "$app/native_ocr.py"
install -m 0644 "$model" "$app/models/$model"
install -m 0755 target/release/lib_planet_ocr.so "$app/_planet_ocr.so"
```

앱이 Rust 모듈을 불러오지 못하면 기존 Python OCR로 시작한답니다. 기존 모델은 앱의 `models/PP-OCRv6_rec_medium.onnx`를 사용한답니다. [ort 프로젝트](https://github.com/pykeio/ort)

## 캡처 재생

`benchmark.py`에 앱 폴더와 정답 JSON 경로를 전달하시와요. JSON은 객체 배열이며 각 항목에 캡처 경로 `image`, 확인된 레벨 `level`, 정답 `expected`를 넣는답니다. 정답은 `[경험치, 퍼센트]`로 쓰고, 읽을 수 없는 캡처에는 `null`을 쓰시와요. `--mode python`과 `--mode rust`로 같은 캡처를 비교할 수 있답니다.
