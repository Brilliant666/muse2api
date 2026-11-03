"""Synthetic, offline identity-parser examples; no real HTML or credentials."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from phase2_identity import IdentityUnverified, authenticated_viewer_id

FIRST = "1000000000000001"
SECOND = "1000000000000002"


def props(principal=FIRST, authenticated=True):
    return {"viewerId": principal, "isAuthenticated": authenticated}


def prelude(principal=FIRST, kind="user"):
    return {"prelude": {"user": {"kind": kind, "identifier": principal}}}


def page(*models, split_at=None):
    flight = "".join(f"{index + 33:x}:" + json.dumps(value) + "\n"
                     for index, value in enumerate(models))
    chunks = [flight] if split_at is None else [flight[:split_at], flight[split_at:]]
    return "<html>" + "".join("<script>self.__next_f.push(" + json.dumps([1, chunk])
                              + ")</script>" for chunk in chunks) + "</html>"


class Phase2IdentityTests(unittest.TestCase):
    def test_authenticated_viewer_requires_matching_user_prelude(self):
        self.assertEqual(authenticated_viewer_id(page(props(), prelude())), FIRST)

    def test_flight_chunks_and_labels_are_not_fixed(self):
        self.assertEqual(authenticated_viewer_id(page(["$", "$L7a", None, props()],
                                                     prelude(), split_at=43)), FIRST)

    def test_missing_or_nonboolean_authentication_is_rejected(self):
        variants = [{"viewerId": FIRST}, props(authenticated=False),
                    props(authenticated="true"), props(authenticated=1)]
        for value in variants:
            with self.subTest(authentication_type=type(value.get("isAuthenticated")).__name__):
                with self.assertRaises(IdentityUnverified):
                    authenticated_viewer_id(page(value, prelude()))

    def test_mismatched_prelude_is_rejected(self):
        with self.assertRaises(IdentityUnverified):
            authenticated_viewer_id(page(props(), prelude(SECOND)))

    def test_multiple_authenticated_principals_are_rejected(self):
        with self.assertRaises(IdentityUnverified):
            authenticated_viewer_id(page(props(), props(SECOND), prelude(), prelude(SECOND)))

    def test_zero_anonymous_or_non_ascii_principals_are_rejected(self):
        for principal in ["0", "0000", "anonymous", "", "１２３", None]:
            with self.subTest(principal_type=type(principal).__name__):
                with self.assertRaises(IdentityUnverified):
                    authenticated_viewer_id(page(props(principal), prelude(principal)))

    def test_missing_or_anonymous_user_prelude_is_rejected(self):
        for models in [(props(),), (props(), prelude(kind="anonymous"))]:
            with self.assertRaises(IdentityUnverified):
                authenticated_viewer_id(page(*models))

    def test_plain_json_or_malformed_flight_does_not_verify(self):
        for html in ["<script>" + json.dumps([props(), prelude()]) + "</script>",
                     "<script>self.__next_f.push([1, not_json])</script>"]:
            with self.assertRaises(IdentityUnverified):
                authenticated_viewer_id(html)


if __name__ == "__main__":
    unittest.main()
