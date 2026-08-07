from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import fetch_citations as fc


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None):
        self._payload = payload if payload is not None else {"response": {"docs": []}}
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self, *, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.posts:
            raise AssertionError("unexpected POST")
        return self.posts.pop(0)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if not self.gets:
            raise AssertionError("unexpected GET")
        return self.gets.pop(0)


class FetchCitationTests(unittest.TestCase):
    def test_module_is_importable_without_token(self):
        self.assertTrue(callable(fc.main))

    def test_main_reports_missing_token_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fc.main(), 1)

    def test_normalize_count_accepts_safe_values_only(self):
        cases = {
            0: 0,
            7: 7,
            4.0: 4,
            " 12 ": 12,
            -1: None,
            2.5: None,
            True: None,
            "2.5": None,
            None: None,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(fc.normalize_count(value), expected)

    def test_fallback_runs_for_every_missing_bigquery_record(self):
        maps = {
            "first_author": {"1": "A", "2": "B", "3": "C"},
            "student": {},
            "coauthor": {},
        }
        bigquery = FakeResponse(
            {"response": {"docs": [
                {"bibcode": "A", "citation_count": 1},
                {"bibcode": "B", "citation_count": 2},
            ]}}
        )
        fallback = FakeResponse(
            {"response": {"docs": [{"bibcode": "C", "citation_count": 3}]}}
        )
        session = FakeSession(posts=[bigquery], gets=[fallback])

        result = fc.collect_citation_counts(
            maps,
            session=session,
            token="token",
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result, {"A": 1, "B": 2, "C": 3})
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(len(session.get_calls), 1)
        self.assertIn('bibcode:"C"', session.get_calls[0][1]["params"]["q"])

    def test_arxiv_alias_resolves_to_requested_website_bibcode(self):
        requested = "2026arXiv260113356G"
        response = FakeResponse(
            {"response": {"docs": [{
                "bibcode": "2026ApJ..1003....1G",
                "citation_count": 9,
                "identifier": ["arXiv:2601.13356", "2026ApJ..1003....1G"],
            }]}}
        )
        session = FakeSession(gets=[response])

        result = fc.fetch_missing(
            [requested],
            session=session,
            token="token",
            sleep=lambda _seconds: None,
        )

        self.assertEqual(result, {requested: 9})
        query = session.get_calls[0][1]["params"]["q"]
        self.assertIn('identifier:"arXiv:2601.13356"', query)

    def test_authorization_error_aborts_all_remaining_chunks(self):
        response = FakeResponse(status_code=401)
        session = FakeSession(gets=[response])
        slept = []
        bibcodes = [f"B{index}" for index in range(fc.FALLBACK_CHUNK_SIZE + 1)]

        result = fc.fetch_missing(
            bibcodes,
            session=session,
            token="bad-token",
            sleep=slept.append,
        )

        self.assertEqual(result, {})
        self.assertEqual(slept, [])
        self.assertEqual(len(session.get_calls), 1)

    def test_transient_429_uses_retry_after_then_succeeds(self):
        first = FakeResponse(status_code=429, headers={"Retry-After": "2"})
        second = FakeResponse(
            {"response": {"docs": [{"bibcode": "A", "citation_count": 5}]}}
        )
        session = FakeSession(gets=[first, second])
        slept = []

        result = fc.fetch_missing(
            ["A"],
            session=session,
            token="token",
            sleep=slept.append,
        )

        self.assertEqual(result, {"A": 5})
        self.assertEqual(slept, [2])
        self.assertEqual(len(session.get_calls), 2)

    def test_section_results_tracks_exact_missing_website_entries(self):
        maps = {
            "first_author": {"1": "A", "2": "B"},
            "student": {"s1": "C"},
            "coauthor": {"1c": "A"},
        }
        sections, missing = fc.section_results(maps, {"A": 7, "C": 0})
        self.assertEqual(sections["first_author"], {"1": 7})
        self.assertEqual(sections["student"], {"s1": 0})
        self.assertEqual(sections["coauthor"], {"1c": 7})
        self.assertEqual(missing, ["first_author:2"])

    def test_complete_output_replaces_old_data(self):
        fresh = {
            "first_author": {"1": 4},
            "student": {"s1": 2},
            "coauthor": {"1c": 8},
        }
        output, complete = fc.build_output(
            fresh,
            [],
            existing={"first_author": {"1": 999}},
            stamp="2026-08-07 01:02 UTC",
        )
        self.assertTrue(complete)
        self.assertEqual(output["first_author"], {"1": 4})
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["updated"], "2026-08-07 01:02 UTC")
        self.assertNotIn("last_attempt", output)
        self.assertNotIn("missing", output)

    def test_stale_output_preserves_last_known_counts_and_records_missing(self):
        existing = {
            "first_author": {"1": 10, "bad": -3},
            "student": {"s1": "4"},
            "coauthor": {"1c": 6},
            "updated": "2026-05-14 11:55 UTC (partial update 2026-05-15 08:52 UTC)",
            "status": "ok",
        }
        fresh = {
            "first_author": {"2": 12},
            "student": {},
            "coauthor": {},
        }

        output, complete = fc.build_output(
            fresh,
            ["first_author:1"],
            existing=existing,
            stamp="2026-08-07 01:02 UTC",
        )

        self.assertFalse(complete)
        self.assertEqual(output["first_author"], {"1": 10, "2": 12})
        self.assertEqual(output["student"], {"s1": 4})
        self.assertEqual(output["coauthor"], {"1c": 6})
        self.assertEqual(output["updated"], "2026-05-14 11:55 UTC")
        self.assertEqual(output["last_attempt"], "2026-08-07 01:02 UTC")
        self.assertEqual(output["status"], "stale")
        self.assertEqual(output["missing"], ["first_author:1"])

    def test_incomplete_output_without_existing_file_is_refused(self):
        with self.assertRaises(fc.CitationUpdateError):
            fc.build_output(
                {"first_author": {}, "student": {}, "coauthor": {}},
                ["first_author:1"],
                existing=None,
                stamp="2026-08-07 01:02 UTC",
            )

    def test_atomic_write_produces_valid_json_and_no_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "citations.json"
            fc.atomic_write_json(path, {"status": "ok", "first_author": {"1": 2}})
            self.assertEqual(json.loads(path.read_text()), {
                "status": "ok",
                "first_author": {"1": 2},
            })
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_main_complete_run_writes_ok_file(self):
        all_counts = {
            bibcode: 1
            for mapping in fc.PUBLICATION_MAPS.values()
            for bibcode in mapping.values()
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "citations.json"
            with (
                patch.dict(
                    os.environ,
                    {"ADS_API_TOKEN": "token", "CITATIONS_OUTPUT": str(output_path)},
                    clear=True,
                ),
                patch.object(fc, "collect_citation_counts", return_value=all_counts),
                redirect_stdout(io.StringIO()),
            ):
                code = fc.main()

            result = json.loads(output_path.read_text())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(set(result["first_author"]), set(fc.FIRST_AUTHOR))
            self.assertNotIn("missing", result)

    def test_main_partial_run_preserves_old_values_and_returns_two(self):
        missing_bibcode = fc.FIRST_AUTHOR["17"]
        partial_counts = {
            bibcode: 2
            for mapping in fc.PUBLICATION_MAPS.values()
            for bibcode in mapping.values()
            if bibcode != missing_bibcode
        }
        existing = {
            section: {key: 9 for key in mapping}
            for section, mapping in fc.PUBLICATION_MAPS.items()
        }
        existing.update({"updated": "2026-08-06 06:00 UTC", "status": "ok"})

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "citations.json"
            output_path.write_text(json.dumps(existing), encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {"ADS_API_TOKEN": "token", "CITATIONS_OUTPUT": str(output_path)},
                    clear=True,
                ),
                patch.object(fc, "collect_citation_counts", return_value=partial_counts),
                redirect_stdout(io.StringIO()),
            ):
                code = fc.main()

            result = json.loads(output_path.read_text())
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "stale")
            self.assertEqual(result["first_author"]["17"], 9)
            self.assertEqual(result["missing"], ["first_author:17"])
            self.assertEqual(result["updated"], "2026-08-06 06:00 UTC")

    def test_duplicate_bibcodes_are_reported(self):
        duplicates = fc.duplicate_bibcodes({
            "first_author": {"1": "A"},
            "student": {"s1": "B"},
            "coauthor": {"1c": "A"},
        })
        self.assertEqual(duplicates, {"A": ["first_author:1", "coauthor:1c"]})


if __name__ == "__main__":
    unittest.main()
