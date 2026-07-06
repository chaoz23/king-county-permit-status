"""Shared city-name detection for permit lookup and routing."""

from __future__ import annotations

import re
from collections.abc import Iterable


def _component_city(component: str, cities: list[str]) -> str | None:
    """Match a locality component, allowing a trailing WA and ZIP code."""
    normalized = " ".join(component.lower().split())
    for city in cities:
        pattern = (
            rf"{re.escape(city)}(?:\s+(?:wa|washington))?"
            rf"(?:\s+\d{{5}}(?:-\d{{4}})?)?"
        )
        if re.fullmatch(pattern, normalized):
            return city
    return None


def detect_city_name(address: str, city_names: Iterable[str]) -> str | None:
    """Detect a city while preferring the locality over words in the street.

    Comma-separated input is treated as a structured address: components after
    the street are checked as localities, and a street-name match is never used
    when those components do not name a supported city. Loose input without
    commas retains the original whole-string fallback.
    """
    cities = sorted(
        {" ".join(city.lower().split()) for city in city_names},
        key=lambda city: (-len(city), city),
    )
    parts = [part.strip() for part in address.split(",") if part.strip()]

    if len(parts) > 1:
        # With two components, the first may itself be just "Seattle". With a
        # normal street/city/state address, the street is the first component.
        locality_parts = parts if len(parts) == 2 else parts[1:]
        for component in reversed(locality_parts):
            city = _component_city(component, cities)
            if city:
                return city
        return None

    normalized = " ".join(address.lower().split())
    for city in cities:
        if re.search(rf"(?<![a-z]){re.escape(city)}(?![a-z])", normalized):
            return city
    return None
