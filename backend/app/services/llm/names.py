"""Conservative full-name matching within evidence, never a person identity resolver."""

import re
from functools import lru_cache

import pymorphy3

WORDS = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*")


def folded(value: str) -> str:
    return value.casefold().replace("ё", "е")


@lru_cache(maxsize=1)
def analyzer():
    return pymorphy3.MorphAnalyzer(lang="ru")


@lru_cache(maxsize=4096)
def parses(word: str, kind: str):
    # No guessed suffix/stem matching. Unknown names keep exact-match behaviour.
    return tuple(p for p in analyzer().parse(word)
                 if p.is_known and kind in p.tag and p.tag.number == "sing")


def _matching_cases(expected, observed, kind, gender):
    targets = {folded(p.normal_form) for p in parses(expected, kind)
               if p.tag.case == "nomn" and p.tag.gender == gender}
    return {p.tag.case for p in parses(observed, kind)
            if p.tag.gender == gender and folded(p.normal_form) in targets}


def person_name_in_quote(name: str, quote: str) -> bool:
    """Match all three parts in order and a common grammatical case/gender.

    Exact tokens (including ё/е and case changes) work for unknown names too.
    Initials, abbreviated names, reversed order and arbitrary fuzzy matches do not.
    """
    target = [match.group() for match in WORDS.finditer(name)]
    observed = [match.group() for match in WORDS.finditer(quote)]
    if not target:
        return False
    # Do not let tokenization silently discard initials, digits or extra punctuation.
    canonical = " ".join(target)
    if folded(" ".join(name.split())) != folded(canonical):
        return False
    for index in range(len(observed) - len(target) + 1):
        window = observed[index:index + len(target)]
        if [folded(w) for w in window] == [folded(w) for w in target]:
            return True
        if len(target) != 3:
            continue
        for gender in ("masc", "femn"):
            common = None
            for expected, actual, kind in zip(target, window, ("Surn", "Name", "Patr")):
                cases = _matching_cases(expected, actual, kind, gender)
                common = cases if common is None else common & cases
                if not common:
                    break
            if common:
                return True
    return False
