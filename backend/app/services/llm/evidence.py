"""Literal evidence checks with token boundaries, not arbitrary substrings."""

import re

from .names import folded


def literal_value_in_text(value: str, text: str, *, email: bool = False) -> bool:
    value = folded(" ".join(value.split()))
    text = folded(" ".join(text.split()))
    boundary = r"[\w@.+-]" if email else r"\w"
    return bool(value and re.search(rf"(?<!{boundary}){re.escape(value)}(?!{boundary})", text))
