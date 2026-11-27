"""Match registry names and abbreviations locally, including ambiguous matches."""

import re


def words(text):
    return re.findall(r"[^\W_]+", (text or "").casefold().replace("ё", "е"))


def lookup_query(question):
    match = re.fullmatch(r"(?:найди|покажи|открой)\s+(?:мне\s+)?карточку\s+(.+?)\s*[?.!]*",
                         question.strip(), re.IGNORECASE)
    return match[1] if match else None


def card_names(card):
    tokens = words(card["name"])
    names = {" ".join(tokens), " ".join(words(card.get("short_name")))}
    legal = {"фгбоу", "фгаоу", "гбпоу", "гапоу", "нияу", "во", "спо"}
    names.update(part.casefold() for part in re.findall(r"\b[А-ЯЁA-Z]{3,}\b", card["name"])
                 if part.casefold() not in legal)
    # For legal names, initials come from the institution name inside quotes.
    quoted = re.findall(r'[«"]([^»"]+)[»"]', card["name"])
    for part in [tokens, *(words(name) for name in quoted)]:
        suffix = next((i for i, word in enumerate(part) if word in {"им", "имени"}), len(part))
        significant = [word for word in part[:suffix] if word not in {"и", "по", "в", "на", "при"}]
        if 2 <= len(significant) <= 10:
            names.add("".join(word[0] for word in significant))
        names.add(" ".join(part))
    return names - {""}


def search_cards(cards, query):
    query = " ".join(words(query))
    if not query:
        return list(cards)
    return [card for card in cards if query in card_names(card)
            or query in " ".join(words(f"{card['name']} {card['city']}"))]


def mentioned_cards(cards, text):
    haystack = f" {' '.join(words(text))} "
    return [card for card in cards if any(f" {name} " in haystack for name in card_names(card))]
