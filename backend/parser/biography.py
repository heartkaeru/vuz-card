"""Attach biography paragraphs to a literal named heading, without changing source text."""

import re

from .models import BiographySubject


def split_biography_sentences(document):
    """Preserve source offsets; names/headings/contacts and their IDs stay intact."""
    fragments = []
    for fragment in document.fragments:
        text = document.text[fragment.start:fragment.end]
        if fragment.contact_hints or text == fragment.heading or len(text) < 180:
            fragments.append(fragment)
            continue
        starts = [0, *[match.end() for match in re.finditer(r"(?<=[а-яёA-Za-z»)])\.\s+(?=[А-ЯЁA-Z])", text)]]
        if len(starts) == 1:
            fragments.append(fragment)
            continue
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            while end > start and text[end - 1].isspace():
                end -= 1
            fragments.append(fragment.model_copy(update={"fragment_id": f"{fragment.fragment_id}_s{i + 1}",
                "start": fragment.start + start, "end": fragment.start + end}))
    document.fragments = fragments


def mark_biography_subject(document, full_name):
    """Only dedicated profile pages; another person's heading ends the scope."""
    normalize = lambda value: " ".join(value.lower().replace("ё", "е").split())
    target = normalize(full_name)
    split_biography_sentences(document)
    anchor = None
    for fragment in document.fragments:
        text = document.text[fragment.start:fragment.end]
        is_heading = text == fragment.heading
        if is_heading and target in normalize(text):
            anchor = fragment.fragment_id
        elif is_heading and re.search(r"[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+\s+[А-ЯЁ][а-яё-]+", text):
            anchor = None
        if anchor:
            fragment.biography_subject = BiographySubject(person_name=full_name, anchor_fragment_id=anchor)
    return any(fragment.biography_subject for fragment in document.fragments)
