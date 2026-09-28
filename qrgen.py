"""
Генерация QR-кодов для конфигов AmneziaWG.

Зачем: на компьютере гораздо удобнее импортировать свой конфиг на смартфон,
сканировав QR с экрана, чем пересылать файл. Используется библиотека segno
(чистый Python, без системных зависимостей) - ставится из requirements.txt.

Если segno на сервере не установлен - функции возвращают None, а бот и сайт
просто молча работают без QR (ничего не ломается).
"""

import io

try:
    import segno
    _SEGNO_OK = True
except ImportError:
    _SEGNO_OK = False


def available() -> bool:
    return _SEGNO_OK


def _normalize_config_for_qr(text: str) -> str:
    """Normalize line endings and remove empty assignments unsupported by some clients."""
    clean = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = []
    for line in clean.split("\n"):
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() and not value.strip():
                continue
        lines.append(line)
    return "\n".join(lines)


def make_qr_png(text: str, scale: int = 8):
    """
    Делает QR-код (PNG-байты) из текста конфигурации.
    Минимальная коррекция (L) уменьшает плотность длинных конфигов; крупный
    scale и стандартная quiet zone помогают сканированию с экрана.
    """
    if not _SEGNO_OK or not text:
        return None
    try:
        # Убираем CR/BOM и пустые key=value назначения вроде "I2 =".
        # Некоторые версии AmneziaWG Android не импортируют такой конфиг по QR.
        clean = _normalize_config_for_qr(text)
        qr = segno.make(clean, error="l")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=scale, dark="#111111", light="#ffffff", border=4)
        return buf.getvalue()
    except Exception:
        return None
