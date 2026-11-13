"""Python adapter for the optional Linux Rust recognizer."""
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace


def open_recognizer(resources):
    from _planet_ocr import Recognizer

    runtime = find_spec('onnxruntime')
    if runtime is None or runtime.origin is None:
        raise ImportError('ONNX Runtime is unavailable')
    libraries = sorted((Path(runtime.origin).parent / 'capi').glob('libonnxruntime.so*'))
    if not libraries:
        raise ImportError('ONNX Runtime shared library is unavailable')
    models = resources / 'models'
    fast = models / 'en_PP-OCRv4_rec_mobile.onnx'
    return NativeOcr(Recognizer(str(libraries[0]), str(models / 'PP-OCRv6_rec_medium.onnx'),
                                str(fast) if fast.is_file() else None))


class NativeOcr:
    def __init__(self, recognizer):
        self.recognizer = recognizer

    def __call__(self, rgb):
        height, width, _ = rgb.shape
        text, score = self.recognizer.text(rgb.tobytes(), width, height)
        return SimpleNamespace(txts=(text,), scores=(score,))

    def read_exp(self, rgb, total):
        return self.recognizer.experience(rgb.tobytes(), rgb.width, rgb.height, total)
