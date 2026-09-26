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


def make_qr_png(text: str, scale: int = 5):
    """
    Делает QR-код (PNG-байты) из текста конфигурации.
    error="h" - максимальный уровень коррекции ошибок (~30%): AmneziaWG-конфиги
    длинные (обфускационные параметры), QR получается плотным, и с таким
    уровнем он увереннее сканируется камерой с экрана, даже если картинка
    чуть уменьшена вёрсткой или бликует. Scale 5 — баланс размера/читаемости.
    """
    if not _SEGNO_OK or not text:
        return None
    try:
        # ЗАЩИТА: на всякий случай сбрасываем возвраты каретки и BOM из текста
        # конфига — иначе AmneziaWG откажется парсить ключ/PSK, а QR будет
        # нечитаемым из-за неожиданных байтов посреди строк.
        clean = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
        qr = segno.make(clean, error="h")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=scale, dark="#111111", light="#ffffff", border=2)
        return buf.getvalue()
    except Exception:
        return None
