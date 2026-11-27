"""Общая настройка понятных русскоязычных журналов приложения."""

import logging
import re
import sys


LEVEL_NAMES = {
    "DEBUG": "ОТЛАДКА",
    "INFO": "ИНФО",
    "WARNING": "ПРЕДУПРЕЖДЕНИЕ",
    "ERROR": "ОШИБКА",
    "CRITICAL": "КРИТИЧЕСКАЯ ОШИБКА",
}


class RussianFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_level = record.levelname
        record.levelname = LEVEL_NAMES.get(original_level, original_level)
        try:
            return super().format(record)
        finally:
            record.levelname = original_level


def configure_logging() -> None:
    """Пишет сообщения приложения в stdout, без шумного HTTP-журнала."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        RussianFormatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(console)

    # В терминале сервера показываем проблемы, а не каждую успешно прочитанную страницу.
    logging.getLogger("parser").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").disabled = True
    for name in ("httpx", "httpcore", "urllib3", "gigachat"):
        logging.getLogger(name).setLevel(logging.WARNING)


ERROR_MESSAGES = {
    "gigachat_http_401": "GigaChat отклонил авторизацию. Проверьте ключ и настройки доступа.",
    "gigachat_http_402": "У GigaChat закончился доступный лимит токенов или средств.",
    "gigachat_http_403": "GigaChat запретил запрос. Проверьте права и параметры аккаунта.",
    "gigachat_http_400": "GigaChat отклонил запрос из-за неверных параметров.",
    "gigachat_http_500": "На стороне GigaChat произошла внутренняя ошибка.",
    "gigachat_http_503": "Сервис GigaChat временно недоступен.",
    "gigachat_http_429": "GigaChat ограничил частоту запросов.",
    "gigachat_timeout": "GigaChat не ответил за отведённое время.",
    "gigachat_connection_failed": "Не удалось подключиться к GigaChat.",
    "gigachat_certificate_verification_failed": "Не удалось проверить сертификат соединения с GigaChat.",
    "gigachat_request_failed": "GigaChat завершил запрос сетевой ошибкой.",
    "incomplete_model_response": "GigaChat вернул неполный ответ.",
    "empty_model_response": "GigaChat вернул пустой ответ.",
    "invalid_extraction_json": "Ответ GigaChat не соответствует ожидаемому формату.",
    "no_source_documents": "Парсер не получил документы для анализа.",
    "empty_source_documents": "Часть полученных страниц не содержит пригодного текста.",
    "collection_cancelled": "Сбор был прерван.",
    "generation_cancelled": "Формирование материалов было прервано.",
    "collection_failed": "Сбор завершился внутренней ошибкой.",
    "material_generation_failed": "Не удалось сформировать материалы.",
    "unexpected_extraction_error": "Во время извлечения фактов произошла внутренняя ошибка.",
}


def describe_error(error: object) -> str:
    """Даёт русское описание кода/исключения, сохраняя неизвестную причину."""

    raw = str(error or "Неизвестная ошибка").strip()
    if raw in ERROR_MESSAGES:
        return ERROR_MESSAGES[raw]
    if match := re.fullmatch(r"gigachat_http_(\d{3})", raw):
        return f"GigaChat вернул HTTP {match.group(1)}."
    if raw.startswith(("Не удалось", "Ошибка", "Достигнут", "robots.txt", "Слишком большой", "Доступ запрещён")):
        return raw
    if re.search(r"\b429\b|Too Many Requests", raw, re.IGNORECASE):
        return "Сайт ограничил частоту запросов."
    if match := re.search(r"\bHTTP\s*(\d{3})\b", raw, re.IGNORECASE):
        return f"Сайт вернул ошибку HTTP {match.group(1)}."
    if "RemoteDisconnected" in raw or "Connection aborted" in raw:
        return "Удалённый сайт закрыл соединение без ответа."
    if "timed out" in raw.lower() or "timeout" in raw.lower():
        return "Истекло время ожидания ответа."
    if "NameResolutionError" in raw or "Failed to resolve" in raw:
        return "Не удалось найти адрес сервера в DNS."
    return f"Ошибка: {raw}"
