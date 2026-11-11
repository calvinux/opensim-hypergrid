import ast
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "webconsole" / "ui"


def catalog_rows():
    source = (UI / "i18n.js").read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    rows = []
    for match in re.finditer(r'^\s*\[\s*"', source, re.MULTILINE):
        try:
            row, _ = decoder.raw_decode(source[match.start():].lstrip())
        except ValueError:
            continue
        if isinstance(row, list) and len(row) == 4 and all(isinstance(item, str) for item in row):
            rows.append(row)
    return rows


class TranslationHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys = []
        self.missing_attributes = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("data-i18n", "data-i18n-placeholder", "data-i18n-aria-label", "data-i18n-title"):
            if key in attrs:
                self.keys.append(attrs[key])
        for attr in ("placeholder", "aria-label", "title"):
            if attr in attrs and "data-i18n-" + attr not in attrs:
                self.missing_attributes.append((tag, attr, attrs[attr]))


class TranslationCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = catalog_rows()
        cls.keys = {row[0] for row in cls.rows}

    def test_static_html_has_translated_text_and_accessibility_attributes(self):
        parser = TranslationHTMLParser()
        parser.feed((UI / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.missing_attributes, [])
        self.assertEqual(sorted(set(parser.keys) - self.keys), [])

    def test_backend_literal_errors_have_translations(self):
        tree = ast.parse((ROOT / "webconsole" / "webconsole.py").read_text(encoding="utf-8"))
        errors = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else function.attr if isinstance(function, ast.Attribute) else ""
            if name not in ("ValueError", "CmdError", "_err"):
                continue
            errors.update(arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        self.assertEqual(sorted(errors - self.keys), [])

    def test_explicit_javascript_messages_have_translations(self):
        source = (UI / "app.js").read_text(encoding="utf-8")
        pattern = r"""\b(?:tr|trf|msg|toast|confirm|prompt)\(\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
        messages = {ast.literal_eval(match.group(1)) for match in re.finditer(pattern, source)}
        self.assertEqual(sorted(messages - self.keys), [])

    def test_all_languages_preserve_template_parameters(self):
        for row in self.rows:
            expected = set(re.findall(r"\{\d+\}", row[0]))
            for value in row[1:]:
                with self.subTest(source=row[0], translation=value):
                    self.assertTrue(value)
                    self.assertEqual(set(re.findall(r"\{\d+\}", value)), expected)


if __name__ == "__main__":
    unittest.main()
