#!/usr/bin/env python3
"""Frontend string parity checks.

The Web UI ships two locales (``zh`` / ``en``) in ``static/i18n.js`` and refers
to them by key from the templates. A key missing from one map fails silently
at runtime — the page falls back to Chinese (or to the raw key), which is
exactly the kind of defect that reaches production unnoticed.

These tests parse the JS statically; no browser is required.
"""

import re
import sys
import unittest
from pathlib import Path

DD_SWITCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DD_SWITCH_DIR))

I18N_JS = DD_SWITCH_DIR / "static" / "i18n.js"
TEMPLATES = [DD_SWITCH_DIR / "templates" / "index.html",
             DD_SWITCH_DIR / "templates" / "login.html"]


def _block_for(source, lang):
    """Return the body of the `lang: { ... }` object in the I18N map."""
    match = re.search(rf"^\s{{4}}{lang}:\s*\{{", source, re.M)
    if not match:
        raise AssertionError(f"no `{lang}` map found in {I18N_JS}")

    start = match.end()
    depth = 1
    index = start
    while depth and index < len(source):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return source[start:index - 1]


def _keys_in(block):
    """Top-level keys of a locale block (6-space indent inside the map)."""
    return set(re.findall(r"^\s{6}([A-Za-z_][A-Za-z0-9_]*)\s*:", block, re.M))


def _template_keys():
    """Every i18n key the templates reference."""
    keys = set()
    for template in TEMPLATES:
        text = template.read_text(encoding="utf-8")
        for attr in ("data-i18n", "data-i18n-placeholder", "data-i18n-title"):
            keys |= set(re.findall(rf'{attr}="([^"]+)"', text))
        keys |= set(re.findall(r'\bt\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*\)', text))
    return keys


class I18nParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = I18N_JS.read_text(encoding="utf-8")
        cls.zh = _keys_in(_block_for(cls.source, "zh"))
        cls.en = _keys_in(_block_for(cls.source, "en"))

    def test_both_locales_define_the_same_keys(self):
        self.assertEqual(self.zh - self.en, set(),
                         "keys missing from the English map")
        self.assertEqual(self.en - self.zh, set(),
                         "keys missing from the Chinese map")

    def test_no_locale_is_empty(self):
        self.assertGreater(len(self.zh), 50)
        self.assertGreater(len(self.en), 50)

    def test_every_template_key_exists_in_both_locales(self):
        used = _template_keys()
        self.assertTrue(used, "no template keys found — the parser is broken")
        self.assertEqual(used - self.zh, set(),
                         "template keys missing from the Chinese map")
        self.assertEqual(used - self.en, set(),
                         "template keys missing from the English map")

    def test_every_error_map_key_exists_in_both_locales(self):
        # ERROR_MAP translates backend error strings via t("<key>").
        referenced = set(re.findall(r'"(err[A-Za-z0-9_]+)"', self.source))
        self.assertTrue(referenced)
        self.assertEqual(referenced - self.zh, set())
        self.assertEqual(referenced - self.en, set())

    def test_every_locale_value_is_a_non_empty_string_or_callable(self):
        for lang, block in (("zh", _block_for(self.source, "zh")),
                            ("en", _block_for(self.source, "en"))):
            entries = re.findall(
                rf"^\s{{6}}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", block, re.M
            )
            self.assertTrue(entries)
            for key, value in entries:
                value = value.strip()
                self.assertTrue(value, f"{lang}.{key} has an empty value")
                self.assertTrue(
                    value.startswith('"') or value.startswith("'")
                    or value.startswith("("),
                    f"{lang}.{key} is neither a string nor a function: {value!r}",
                )


if __name__ == "__main__":
    unittest.main()
