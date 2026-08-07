from __future__ import annotations

import re
import unittest
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

import fetch_citations as fc


ROOT = Path(__file__).resolve().parents[1]


class SiteParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.frames = []
        self.current_section = None
        self.current_publication = None
        self.ids = []
        self.inline_handlers = []
        self.anchors_without_href = []
        self.tab_ids = []
        self.nav_hashes = []
        self.publication_keys = defaultdict(list)
        self.publication_links = defaultdict(list)
        self.script_blocks = []
        self._script_parts = None

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        previous_section = self.current_section
        previous_publication = self.current_publication
        if tag not in self.VOID_TAGS:
            self.frames.append((tag, previous_section, previous_publication))

        if "id" in attributes:
            self.ids.append(attributes["id"])
        for name in attributes:
            if name.lower().startswith("on"):
                self.inline_handlers.append((tag, name))

        classes = set((attributes.get("class") or "").split())
        section_name = attributes.get("data-citation-section")
        if section_name:
            self.current_section = section_name

        if tag == "section" and "tab-content" in classes and attributes.get("id"):
            self.tab_ids.append(attributes["id"])

        if tag == "div" and "pub-item" in classes and self.current_section:
            key = attributes.get("data-citation-key")
            self.current_publication = (self.current_section, key)
            self.publication_keys[self.current_section].append(key)

        if tag == "a":
            href = attributes.get("href")
            if href is None:
                self.anchors_without_href.append(attributes)
            if href and href.startswith("#") and "nav-links" in self._ancestor_classes():
                self.nav_hashes.append(href[1:])
            if self.current_publication and href:
                self.publication_links[self.current_publication].append(href)

        if tag == "script" and "src" not in attributes:
            self._script_parts = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._script_parts is not None:
            self._script_parts.append(data)

    def handle_entityref(self, name):
        if self._script_parts is not None:
            self._script_parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._script_parts is not None:
            self._script_parts.append(f"&#{name};")

    def handle_endtag(self, tag):
        if tag == "script" and self._script_parts is not None:
            self.script_blocks.append("".join(self._script_parts))
            self._script_parts = None

        if not self.frames:
            return
        frame_tag, previous_section, previous_publication = self.frames.pop()
        if frame_tag != tag:
            # The supplied document is expected to be well nested. Keep parsing
            # deterministic so a mismatch becomes a test failure below.
            self.frames.append((frame_tag, previous_section, previous_publication))
            return
        self.current_section = previous_section
        self.current_publication = previous_publication

    def _ancestor_classes(self):
        # HTMLParser does not retain attributes in frames; nav links are all the
        # hash links before the hero in this single-file site, so use a marker
        # populated from raw source in the test instead of this helper.
        return set()


class SiteIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_text = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.parser = SiteParser()
        cls.parser.feed(cls.html_text)
        cls.parser.close()

    def test_document_has_closing_tags_and_one_inline_script(self):
        self.assertRegex(self.html_text.rstrip(), r"</body>\s*</html>$")
        self.assertEqual(len(self.parser.script_blocks), 1)
        self.assertEqual(self.parser.frames, [])

    def test_ids_are_unique_and_no_inline_event_handlers_remain(self):
        duplicates = [item for item, count in Counter(self.parser.ids).items() if count > 1]
        self.assertEqual(duplicates, [])
        self.assertEqual(self.parser.inline_handlers, [])
        self.assertEqual(self.parser.anchors_without_href, [])

    def test_displayed_publication_numbers_match_stable_keys(self):
        pairs = re.findall(
            r'<div class="pub-item" data-citation-key="([^"]+)">\s*'
            r'<span class="pub-year">\s*([^<]+?)\s*</span>',
            self.html_text,
        )
        self.assertEqual(len(pairs), sum(len(mapping) for mapping in fc.PUBLICATION_MAPS.values()))
        for key, displayed in pairs:
            expected = key[1:] if key.startswith("s") else key[:-1] if key.endswith("c") else key
            with self.subTest(key=key):
                self.assertEqual(displayed.rstrip("."), expected)

    def test_publication_dom_keys_exactly_match_python_maps(self):
        for section, mapping in fc.PUBLICATION_MAPS.items():
            with self.subTest(section=section):
                keys = self.parser.publication_keys[section]
                self.assertEqual(Counter(keys), Counter(mapping.keys()))

    def test_publication_links_are_not_empty_or_placeholder_urls(self):
        for publication, links in self.parser.publication_links.items():
            with self.subTest(publication=publication):
                self.assertGreaterEqual(len(links), 1)
                self.assertNotIn("#", links)
                self.assertTrue(all(link.strip() for link in links))

    def test_tab_links_have_real_hash_targets(self):
        expected = set(self.parser.tab_ids)
        nav_block = re.search(r'<ul class="nav-links".*?</ul>', self.html_text, re.S)
        self.assertIsNotNone(nav_block)
        nav_targets = re.findall(r'href="#([^"]+)"', nav_block.group(0))
        self.assertEqual(set(nav_targets), expected)
        self.assertEqual(len(nav_targets), len(expected))

        hero_block = re.search(r'<div class="hero-nav-hint">.*?</div>', self.html_text, re.S)
        self.assertIsNotNone(hero_block)
        hero_targets = re.findall(r'href="#([^"]+)"', hero_block.group(0))
        self.assertTrue(hero_targets)
        self.assertTrue(set(hero_targets).issubset(expected))

    def test_extracted_script_is_synchronized_and_parses_with_node_when_available(self):
        extracted = (ROOT / "site-scripts.extracted.js").read_text(encoding="utf-8")
        extracted_body = extracted.split("*/", 1)[1].lstrip()
        inline = self.parser.script_blocks[0].strip() + "\n"
        self.assertEqual(extracted_body, inline)

    def test_citation_status_and_stable_data_hooks_exist(self):
        self.assertEqual(self.html_text.count('id="citation-status"'), 1)
        for section in fc.PUBLICATION_MAPS:
            self.assertEqual(
                self.html_text.count(f'data-citation-section="{section}"'),
                1,
            )
        self.assertNotIn("onclick=", self.html_text.lower())
        self.assertNotIn("data-tab=", self.html_text.lower())
        script = self.parser.script_blocks[0]
        self.assertNotIn("nextElementSibling", script)
        self.assertNotIn("replace(/^s/", script)
        self.assertNotIn("First Author Papers", script)

    def test_workflow_uses_current_official_actions_and_safety_guards(self):
        workflow = (ROOT / ".github/workflows/update-citations.yml").read_text(encoding="utf-8")
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("timeout-minutes:", workflow)
        self.assertIn("concurrency:", workflow)

    def test_known_fixed_ads_links(self):
        self.assertIn(
            "https://ui.adsabs.harvard.edu/abs/2022ApJ...926..108C/abstract",
            self.parser.publication_links[("coauthor", "33")],
        )
        self.assertIn(
            "https://ui.adsabs.harvard.edu/abs/2025JKAS...58...17C/abstract",
            self.parser.publication_links[("coauthor", "72")],
        )


if __name__ == "__main__":
    unittest.main()
