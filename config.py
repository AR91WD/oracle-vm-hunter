import importlib.util
from pathlib import Path
_Path = Path

# =========================================================
# DEFAULT CONFIGURATION
# =========================================================
# Эти значения будут использоваться, если они не переопределены в вашем config-файле.

# =========================================================
#  Paths
# =========================================================
BASE_DIR = Path.cwd()
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = OUTPUT_DIR / "logs"
SUPPORTED_EXTENSIONS = [".mp4", ".mov", ".mkv", ".mp3", ".wav", ".m4a"]

# =========================================================
#  Model
# =========================================================
MODEL_NAME = "large-v3"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"

# =========================================================
#  Languages
# =========================================================
LANGUAGE = None  # None для автоопределения, или "ru", "fr", "en"

# =========================================================
#  Whisper
# =========================================================
BEAM_SIZE = 5

# =========================================================
#  VAD (Voice Activity Detection)
# =========================================================
VAD_FILTER = True

# =========================================================
#  Post-processing
# =========================================================
MIN_TEXT_LENGTH = 2


def load_config(config_path: str):
    """Загружает конфигурацию из Python-файла."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    spec = importlib.util.spec_from_file_location(config_path.stem, config_path)
    config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_module)

    # Применяем настройки из файла поверх дефолтных
    for key in dir(config_module):
        if key.isupper():
            globals()[key] = getattr(config_module, key)
    
    return sys.modules[__name__]

def get_output_path(input_file: str, suffix: str, output_dir: Path):
    """Создает путь для выходного файла."""
    base_name = Path(input_file).stem
    return output_dir / f"{base_name}{suffix}"

import sys
import os
import urllib.parse
import urllib.request

import subprocess
import shutil
import tempfile
import re
import json
from typing import List

# локальные импорты (не должны вызывать циклические зависимости)
try:
    # импортим здесь, чтобы не создавать циклы при тестах/анализе
    from models import Segment, TranscriptionResult
    import utils
    from logger import get_logger, log_start_session
except Exception:
    # при статическом анализе может не быть контекста — продолжим, ошибки будут при запуске
    Segment = None
    TranscriptionResult = None
    utils = None
    get_logger = None
    log_start_session = None


def _ensure_models():
    global Segment, TranscriptionResult
    if Segment is None or TranscriptionResult is None:
        from models import Segment as _Seg, TranscriptionResult as _TR
        Segment = _Seg
        TranscriptionResult = _TR


def _ensure_utils_and_logger():
    global utils, get_logger, log_start_session
    if utils is None:
        import utils as _utils
        utils = _utils
    if get_logger is None or log_start_session is None:
        from logger import get_logger as _get_logger, log_start_session as _log_start
        get_logger = _get_logger
        log_start_session = _log_start


def _ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_audio(input_path: str, out_path: str) -> str:
    """Извлекает аудио из видео с помощью ffmpeg. Возвращает путь к аудиофайлу."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден в PATH. Установите ffmpeg для извлечения аудио.")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(out_path)


def _choose_stt_backend():
    """Возвращает строку: 'faster_whisper'|'whisper' или None"""
    try:
        import faster_whisper  # type: ignore

        return "faster_whisper"
    except Exception:
        pass

    try:
        import whisper  # type: ignore

        return "whisper"
    except Exception:
        pass

    return None


def transcribe_audio(audio_path: str, logger=None) -> TranscriptionResult:
    """Транскрибирует аудиофайл, возвращает TranscriptionResult.

    Попытка: faster-whisper -> whisper. Если ничего нет, бросаем понятную ошибку.
    """
    _ensure_models()
    backend = _choose_stt_backend()
    if backend is None:
        raise RuntimeError(
            "Нет доступного STT-бэкенда. Установите faster-whisper или openai/whisper."
        )

    if logger:
        logger.info(f"Using STT backend: {backend}")

    segments = []
    total_text = ""

    if backend == "faster_whisper":
        from faster_whisper import WhisperModel  # type: ignore

        model = WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
        # faster-whisper возвращает (segments_iterable, info)
        options = dict(beam_size=BEAM_SIZE)
        segments_iter, info = model.transcribe(audio_path, **options)
        for segment in segments_iter:
            # segment: has attributes start, end, text, language
            seg = Segment(start=segment.start, end=segment.end, text=getattr(segment, 'text', '').strip(), language=getattr(segment, "language", None))
            segments.append(seg)
            total_text += " " + seg.text

    else:
        import whisper  # type: ignore

        model = whisper.load_model(MODEL_NAME)
        result = model.transcribe(audio_path, beam_size=BEAM_SIZE)
        for s in result.get("segments", []):
            seg = Segment(start=s.get("start", 0.0), end=s.get("end", 0.0), text=s.get("text", "").strip(), language=result.get("language"))
            segments.append(seg)
            total_text += " " + seg.text

    return TranscriptionResult(text=total_text.strip(), segments=segments, language=None, duration=None, source_file=audio_path)


FILLERS = {
    "ru": ["эм", "ээм", "как бы", "типа", "ну", "в общем"],
    "en": ["um", "uh", "like", "you know", "I mean"],
    "fr": ["euh", "bah", "genre", "tu vois"]
}


def clean_text(text: str, lang: str = "en") -> str:
    """Простейшая очистка: удаляет слова-паразиты, повторяющиеся слова и шумовые токены."""
    if not text:
        return text

    t = text.strip()
    # удаляем шумовые маркеры
    t = re.sub(r"\[.*?\]|\(.*?\)", "", t)

    # удаляем повторы подряд (слово слово слово -> слово)
    t = re.sub(r"\b(\w+)(\s+\1\b)+", r"\1", t, flags=re.IGNORECASE)

    # убираем простые filler-слова
    for f in FILLERS.get(lang[:2], []):
        pattern = r"\b" + re.escape(f) + r"\b"
        t = re.sub(pattern, "", t, flags=re.IGNORECASE)

    # убираем лишние пробелы
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def merge_segments(segments: List[Segment], max_gap: float = 0.8) -> List[Segment]:
    """Объединяет соседние сегменты с малым промежутком и одинаковым языком"""
    if not segments:
        return []

    merged = []
    cur = segments[0]
    for s in segments[1:]:
        same_lang = (cur.language or "") == (s.language or "")
        gap = s.start - cur.end
        if same_lang and gap <= max_gap:
            # объединяем
            cur = Segment(start=cur.start, end=s.end, text=(cur.text + " " + s.text).strip(), language=cur.language)
        else:
            merged.append(cur)
            cur = s
    merged.append(cur)
    return merged


def export_transcript(result: TranscriptionResult, out_file: _Path):
    """Записывает файл в требуемом формате:
    [00:00:01] [RU] текст [00:00:05] [FR] texte ...
    Каждая смысловая фраза в отдельной строке.
    """
    _ensure_utils_and_logger()
    lines = []
    merged = merge_segments(result.segments)
    for seg in merged:
        ts = utils.short_timestamp(seg.start) if utils else short_timestamp(seg.start)
        lang = seg.language or ""
        lang_tag = "[EN]"
        if lang:
            code = lang[:2].lower()
            if code == "ru":
                lang_tag = "[RU]"
            elif code == "fr":
                lang_tag = "[FR]"
            else:
                lang_tag = "[EN]"

        cleaned = clean_text(seg.text, lang=lang or "en")
        lines.append(f"[{ts}] {lang_tag} {cleaned}")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def run_pipeline(input_path: str, output_file: str = None, logger=None):
    """Полный pipeline: video -> audio -> stt -> postprocess -> export"""
    _ensure_utils_and_logger()
    if logger is None and get_logger:
        logger = get_logger()

    if logger and log_start_session:
        log_start_session(logger, input_path)

    _ensure_output_dir()
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(input_path).with_suffix(".wav")
        tmp_audio = _Path(tmp) / (Path(input_path).stem + ".wav")
        audio_path_str = extract_audio(input_path, tmp_audio)

        result = transcribe_audio(audio_path_str, logger=logger)

        if output_file is None:
            output_file = OUTPUT_DIR / "output_transcript.txt"
        else:
            output_file = _Path(output_file)

        export_transcript(result, output_file)

    if logger:
        logger.info(f"Transcript written to: {output_file}")
    _notify_telegram("Транскрибация завершена", f"Файл готов: {output_file}")


def _notify_telegram(subject: str, message: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    text = f"{subject}\n{message}"
    data = {
        "chat_id": chat_id,
        "text": text,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception:
        pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transcriber pipeline (Video -> Transcript)")
    parser.add_argument("input", help="входной видео/аудио файл")
    parser.add_argument("--out", help="выходной файл транскрипции", default=None)
    args = parser.parse_args()

    logger = get_logger() if get_logger else None
    try:
        run_pipeline(args.input, args.out, logger=logger)
    except Exception as e:
        if logger:
            logger.exception("Pipeline failed")
        else:
            print("Pipeline failed:", e)
        _notify_telegram("Ошибка транскрибации", f"Файл: {args.input}\nОшибка: {e}")
