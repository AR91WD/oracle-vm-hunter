from pathlib import Path

# ---------------------------------------------------------
# Время
# ---------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """Форматирует время в формат SRT: 00:00:00,000"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)

    if millis == 1000:
        millis = 0
        secs += 1

    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def short_timestamp(seconds: float) -> str:
    """Форматирует время в короткий формат: 00:00:00"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


# ---------------------------------------------------------
# Язык
# ---------------------------------------------------------

LANGUAGE_NAMES = {
    "fr": "FR",
    "ru": "RU",
    "en": "EN"
}


def language_name(code: str) -> str:
    """Возвращает короткое имя языка."""
    return LANGUAGE_NAMES.get(code.lower(), code.upper())
