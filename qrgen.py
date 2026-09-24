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


def make_qr_png(text: str, scale: int = 6):
    """
    Делает QR-код (PNG-байты) из текста конфигурации.
    error="m" - средний уровень коррекции ошибок: QR получается не слишком плотным
    и уверенно сканируется камерой даже с экрана ноутбука.
    """
    if not _SEGNO_OK or not text:
        return None
    try:
        qr = segno.make(text, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=scale, dark="#111111", light="#ffffff")
        return buf.getvalue()
    except Exception:
        return None
