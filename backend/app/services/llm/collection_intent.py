"""Validate the scope of an assistant-proposed collection before creating a job."""

import re
from urllib.parse import urlsplit

from parser.public_http import public_url
from pydantic import HttpUrl

from .card_lookup import card_names, mentioned_cards, words
from .changes import normalized


COLLECTION_WORD = r"\b(?:обнов\w*|актуализ\w*|сбор\w*|собра\w*|собери\w*|пересоб\w*|найти|поиск\w*)\b"
HOW_QUESTION = re.compile(r"\b(?:как|каким способом|где|какую кнопку)\s+(?:[\w-]+\s+){0,5}" + COLLECTION_WORD)
NEGATED_COLLECTION = re.compile(
    r"\bне\s+(?:(?:надо|нужно|следует|хочу)\s+)?"
    r"(?:обнов\w*|актуализ\w*|собира\w*|собери\w*|пересоб\w*|запуска\w*|запусти\w*)"
)
TARGET = re.compile(r"\b(?:сведения|данные|инфу|информацию|карточку)\s+(?:(?:о|об|по|для)\s+)?([^.!?;,]+)")
URL = re.compile(r"https?://[^\s<>«»\"']+", re.I)
DIRECTIVE = re.compile(
    r"\b(?:(?:обнови|актуализируй|собери|пересобери|найди|поищи|проверь|сверь|запусти)(?:те)?"
    r"|(?:прошу|можешь|можете)\s+(?:обновить|собрать|проверить|пересобрать))\b"
)
DIRECT_TARGET = re.compile(r"^(?:обнови|актуализируй)(?:те)?\s+([^.!?;,]+)")
OPEN_TARGET = re.compile(
    r"^(?:(?:этого|этом|этому|текущего|текущем|текущему|открытого|открытом|открытому)\s+)?"
    r"(?:вуз\w*|университет\w*|колледж\w*|институт\w*|учреждени\w*|организаци\w*|"
    r"на\s+сайте|с\s+(?:официального\s+)?сайта|официально\w*\s+сайт\w*|"
    r"официально\w*\s+страниц\w*|с\s+(?:официальной\s+)?страницы|"
    r"в\s+(?:этой\s+)?карточке|по\s+открытой\s+карточке|открытой\s+карточке|"
    r"по\s+ссылке|ссылке|с\s+нуля|заново|ещ[её]\s+раз|с)(?:\s|$)"
)


def collection_help(question):
    if HOW_QUESTION.search(normalized(question)):
        return ("Откройте нужную карточку и нажмите «Обновить данные» или попросите агента запустить сбор. "
                "После завершения откройте «Проверить факты».")
    return None


def collection_blocker(question):
    if NEGATED_COLLECTION.search(normalized(question)):
        return "По этой просьбе сбор не запускаю. Могу ответить по сохранённым сведениям."
    return collection_help(question)


def named_update_query(question):
    text = normalized(URL.sub("", question)).rstrip(" .!?;")
    command = DIRECT_TARGET.match(text)
    if not command:
        return None
    target = re.sub(r"^(?:сведения|данные|инфу|информацию|карточку)(?:\s+|$)", "", command[1])
    target = re.sub(r"^(?:о|об|по|для)\s+", "", target).strip()
    return "" if OPEN_TARGET.match(target) else target


def refresh_scope(question, card, cards):
    question = URL.sub("", question).strip()
    targets = mentioned_cards(cards, question)
    if any(target["id"] != card["id"] for target in targets):
        return {"text": f"Сейчас открыта карточка «{card['name']}». Выберите нужное учреждение для обновления.",
                "sources": [], "cards": targets[:10]}
    if not DIRECTIVE.search(normalized(question)):
        return {"text": "Чтобы запустить сбор по открытой карточке, попросите обновить сведения или нажмите «Обновить данные».",
                "sources": []}
    if targets or mentioned_cards([card], question):
        return None

    # A named target that cannot be tied to the open card requires clarification.
    text = normalized(question).rstrip(" .!?;")
    target = TARGET.search(text) or (None if re.search(r"\b(?:сведения|данные|инфу|информацию|карточку)$", text)
                                  else DIRECT_TARGET.search(text))
    remaining = " ".join(word for word in words(target[1]) if word != "пожалуйста") if target else ""
    uppercase_name = any(token.isupper() and token.isalpha() and len(token) >= 3
                         for token in re.findall(r"[^\W_]+", question))
    if uppercase_name or (remaining and not OPEN_TARGET.match(remaining)):
        return {"text": f"Подтвердите учреждение. Сейчас открыта карточка «{card['name']}». "
                        "Если имелся в виду этот вуз, попросите собрать сведения по открытой карточке.",
                "sources": []}
    return None


def refresh_sources(question, website):
    host = (urlsplit(website).hostname or "").removeprefix("www.").casefold()
    seen = set()
    for match in URL.finditer(question):
        url = str(HttpUrl(match[0].rstrip(".,;!?)")))
        public_url(url)
        source_host = (urlsplit(url).hostname or "").removeprefix("www.").casefold()
        if not host or (source_host != host and not source_host.endswith("." + host)):
            raise ValueError("official_source_required")
        seen.add(url)
        if len(seen) > 3:
            raise ValueError("too_many_sources")
    return sorted(seen)
