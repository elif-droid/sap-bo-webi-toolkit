"""Text normalisation used for every name comparison in the toolkit."""

from __future__ import annotations

import re
import unicodedata

_SPECIAL = str.maketrans({"ı": "i", "İ": "I", "ß": "ss", "\xa0": " "})


def norm(s) -> str:
    """Accent-insensitive, case-insensitive, whitespace-collapsed form of a string.

    'Crème  Brûlée' == 'creme brulee'. BO often returns names with non-breaking or
    doubled spaces, so they are collapsed too.
    """
    s = str(s or "").translate(_SPECIAL)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s.casefold().strip()


def norm_upper(s) -> str:
    return norm(s).upper()


def parse_list(text: str) -> tuple[str, ...]:
    """'a, b; c\\nd' -> ('a', 'b', 'c', 'd') normalised."""
    return tuple(norm(x) for x in re.split(r"[,;\n]", text or "") if x.strip())


def contains_any(name: str, needles) -> bool:
    n = norm(name)
    return any(x and x in n for x in (needles or ()))
