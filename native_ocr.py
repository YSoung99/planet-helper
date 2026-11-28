"""Python adapter for the optional Linux Rust recognizer."""
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


def _extension_candidates(resources):
    """Search the current release, the persistent install root, and data dir."""
    roots = [Path(resources)]
    install_root = os.environ.get('PLANET_HELPER_INSTALL_ROOT')
    if install_root:
        roots.append(Path(install_root))
    data_dir = os.environ.get('PLANET_EXP_DATA_DIR')
    if data_dir:
        roots.append(Path(data_dir))
    seen = set()
    for root in roots:
        resolved = root.resolve() if root.exists() else root
        if resolved in seen:
            continue
        seen.add(resolved)
        exact = root / '_planet_ocr.so'
        if exact.is_file():
            yield exact
        else:
            for candidate in sorted(root.glob('_planet_ocr*.so')):
                if candidate.is_file():
                    yield candidate


def _load_recognizer_class(resources):
    try:
        from _planet_ocr import Recognizer
        return Recognizer
    except ModuleNotFoundError as exc:
        if exc.name != '_planet_ocr':
            raise
    for path in _extension_candidates(resources):
        spec = spec_from_file_location('_planet_ocr', str(path))
        if spec is None or spec.loader is None:
            continue
        module = module_from_spec(spec)
        sys.modules['_planet_ocr'] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop('_planet_ocr', None)
            raise
        return module.Recognizer
    raise ModuleNotFoundError("No module named '_planet_ocr'", name='_planet_ocr')


def _find_model(resources, filename):
    candidates = [Path(resources) / 'models' / filename]
    install_root = os.environ.get('PLANET_HELPER_INSTALL_ROOT')
    if install_root:
        candidates.append(Path(install_root) / 'models' / filename)
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def open_recognizer(resources):
    Recognizer = _load_recognizer_class(resources)

    runtime = find_spec('onnxruntime')
    if runtime is None or runtime.origin is None:
        raise ImportError('ONNX Runtime is unavailable')
    libraries = sorted((Path(runtime.origin).parent / 'capi').glob('libonnxruntime.so*'))
    if not libraries:
        raise ImportError('ONNX Runtime shared library is unavailable')
    medium = _find_model(resources, 'PP-OCRv6_rec_medium.onnx')
    fast = _find_model(resources, 'en_PP-OCRv4_rec_mobile.onnx')
    return NativeOcr(Recognizer(str(libraries[0]), str(medium),
                                str(fast) if fast.is_file() else None))


class NativeOcr:
    def __init__(self, recognizer):
        self.recognizer = recognizer
        self.lock = threading.RLock()

    def __call__(self, rgb):
        height, width, _ = rgb.shape
        with self.lock:
            text, score = self.recognizer.text(rgb.tobytes(), width, height)
        return SimpleNamespace(txts=(text,), scores=(score,))

    def read_exp(self, rgb, total):
        with self.lock:
            return self.recognizer.experience(rgb.tobytes(), rgb.width, rgb.height, total)
