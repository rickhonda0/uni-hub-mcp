"""Parse unit codes, terms, weeks and document kinds out of course text."""
from __future__ import annotations

import re
from collections.abc import Iterable

_CODE = re.compile(r"(?<![A-Za-z])([A-Z]{4})(\d{4})((?:\s*/\s*\d{4})*)(?!\d)")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_SEMESTER = re.compile(r"(?:semester|sem|(?<![A-Za-z])s)\s*([12])(?!\d)", re.I)
_WEEK = re.compile(r"(?<![A-Za-z])(?:week|wk|w)[\s_.\-]*(\d{1,2})(?!\d)", re.I)
_SOLUTION = re.compile(r"solution|answers?(?![a-z])|(?<![a-z])sols?(?![a-z])|worked", re.I)

# Checked in order; the first match wins. Patterns run on lowercased text with _ - . turned into spaces.
KIND_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("exam", re.compile(r"exam|past paper|practice paper|mock test|mid ?sem")),
    ("quiz", re.compile(r"(?<![a-z])quiz")),
    ("tutorial", re.compile(
        r"tutorial|(?<![a-z])tut(?![a-z])|workshop|(?<![a-z])lab(?![a-z])|practical"
        r"|problem (?:set|sheet)|exercise|worksheet")),
    ("lecture", re.compile(r"lecture|(?<![a-z])lec(?![a-z])|(?<![a-z])slides?(?![a-z])|(?<![a-z])notes(?![a-z])")),
    ("assignment", re.compile(
        r"assignment|assessment|project|(?<![a-z])spec(?![a-z])|specification"
        r"|(?<![a-z])report(?![a-z])|proposal|rubric|marking")),
    ("reading", re.compile(r"reading|textbook|chapter")),
]


def unit_codes(*texts: str | None) -> list[str]:
    """'MATH1962/1972 (ND)' -> ['MATH1962', 'MATH1972']."""
    codes: list[str] = []
    for text in texts:
        for match in _CODE.finditer(text or ""):
            numbers = [match.group(2), *re.findall(r"\d{4}", match.group(3) or "")]
            for number in numbers:
                code = match.group(1) + number
                if code not in codes:
                    codes.append(code)
    return codes


def term_key(text: str | None) -> tuple[int, int] | None:
    """'Semester 2 2026' or '2023_S1C' -> (2026, 2) / (2023, 1)."""
    if not text:
        return None
    year, sem = _YEAR.search(text), _SEMESTER.search(text)
    if year and sem:
        return int(year.group(1)), int(sem.group(1))
    return None


def parse_week(*texts: str | None) -> int | None:
    for text in texts:
        match = _WEEK.search(text or "")
        if match and 0 <= int(match.group(1)) <= 15:
            return int(match.group(1))
    return None


def _normalise(text: str | None) -> str:
    return re.sub(r"[_\-.]+", " ", (text or "").lower())


def classify(texts: Iterable[str | None], default: str = "other") -> str:
    """Return the first kind matched, checking the most specific text (e.g. the title) first."""
    for text in texts:
        norm = _normalise(text)
        for kind, pattern in KIND_RULES:
            if pattern.search(norm):
                return kind
    return default


def is_solution(text: str | None) -> bool:
    return bool(_SOLUTION.search(_normalise(text)))


def slug(text: str | None, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")[:limit] or "untitled"
