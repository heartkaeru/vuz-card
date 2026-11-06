"""Explicit scopes for reference pages; article/project identities remain independent."""

import re


def mark_institution_context(document, name):
    if document.section not in {"about", "organization"}:
        return False
    fold = lambda text: " ".join(text.lower().replace("ё", "е").split())
    # A literal name in the cleaned source is required, not just the requested card.
    candidates = [f for f in document.fragments if fold(name) in fold(document.text[f.start:f.end])
                  and f.end - f.start <= 1500]
    # A report's literal title can establish both institution and reporting period.
    # It is not the publication/retrieval date or a year inferred from the filename.
    anchor = next((f for f in candidates if document.file_sha256 and re.search(
        r"\bза\s+20\d{2}\s+год", fold(document.text[f.start:f.end]))), None)
    anchor = anchor or next(iter(candidates), None)
    if anchor is None:
        return False
    for fragment in document.fragments:
        fragment.institution_anchor_id = anchor.fragment_id
    return True
