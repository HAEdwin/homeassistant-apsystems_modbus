"""Tests for the translation files."""

from __future__ import annotations

import json
from pathlib import Path
import re

TRANSLATIONS = (
    Path(__file__).parent.parent
    / "custom_components"
    / "apsystems_modbus"
    / "translations"
)
PLACEHOLDER = re.compile(r"{(\w+)}")


def _strings(node: object, path: str = "") -> dict[str, str]:
    """Flatten a translation file to dotted keys."""
    if isinstance(node, str):
        return {path: node}
    assert isinstance(node, dict)
    flat: dict[str, str] = {}
    for key, value in node.items():
        flat.update(_strings(value, f"{path}.{key}" if path else key))
    return flat


def test_translations_use_the_english_placeholders() -> None:
    """Home Assistant falls back to English when the placeholders differ."""
    english = _strings(json.loads((TRANSLATIONS / "en.json").read_text()))
    for file in TRANSLATIONS.glob("*.json"):
        translated = _strings(json.loads(file.read_text()))
        for key, text in translated.items():
            assert key in english, f"{file.name}: {key} is not in en.json"
            assert set(PLACEHOLDER.findall(text)) == set(
                PLACEHOLDER.findall(english[key])
            ), f"{file.name}: {key}"
