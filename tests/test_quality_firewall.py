"""Unit tests for deterministic Quality Firewall v1 primitives."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services import (
    QUALITY_EVIDENCE_MAX_BYTES,
    bound_quality_evidence,
    compute_quality_score,
    compute_quality_score_breakdown,
    quality_normalization_hash_version,
    run_normalization_pipeline,
    sha256_text,
)


class QualityFirewallPrimitiveTest(unittest.TestCase):
    """Verify stable hashes, normalization semantics, scoring, and evidence."""

    def test_sha256_raw_is_stable_and_uses_exact_utf8_bytes(self):
        source = "Ligne une\r\nLigne deux — été"
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()

        self.assertEqual(sha256_text(source), expected)
        self.assertEqual(sha256_text(source), sha256_text(source))
        self.assertNotEqual(sha256_text(source), sha256_text(source + " "))

    def test_different_raw_content_can_have_the_same_normalized_hash(self):
        crlf_source = "# Titre\r\n\r\nTexte stable"
        lf_source = "# Titre\n\nTexte stable"
        first = run_normalization_pipeline(crlf_source)["normalized_content"]
        second = run_normalization_pipeline(lf_source)["normalized_content"]

        self.assertNotEqual(sha256_text(crlf_source), sha256_text(lf_source))
        self.assertEqual(first, second)
        self.assertEqual(sha256_text(first), sha256_text(second))
        self.assertEqual(
            quality_normalization_hash_version("v2"),
            "v2:utf8-sha256/v1",
        )

    def test_really_different_documents_have_different_normalized_hashes(self):
        first = run_normalization_pipeline("Document alpha utile.")[
            "normalized_content"
        ]
        second = run_normalization_pipeline("Document beta sans rapport.")[
            "normalized_content"
        ]

        self.assertNotEqual(sha256_text(first), sha256_text(second))

    def test_score_breakdown_preserves_the_existing_score_formula(self):
        text = " ".join(f"mot{index}" for index in range(80))
        breakdown = compute_quality_score_breakdown(text)

        self.assertEqual(breakdown["score"], compute_quality_score(text))
        self.assertEqual(
            breakdown["score"],
            round(sum(item["score_delta"] for item in breakdown["components"]), 4),
        )
        self.assertEqual(
            {item["rule_code"] for item in breakdown["components"]},
            {"QF_LENGTH_SCORE", "QF_LEXICAL_DIVERSITY", "QF_CHARACTER_SIGNAL"},
        )

    def test_evidence_is_bounded_without_retaining_document_content(self):
        private_content = "DONNEE_PRIVEE_" * 500
        bounded = bound_quality_evidence(
            {"document_content": private_content, "token_count": 42}
        )
        serialized = json.dumps(
            bounded,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        self.assertLessEqual(len(serialized), QUALITY_EVIDENCE_MAX_BYTES)
        self.assertNotIn(private_content.encode("utf-8"), serialized)
        self.assertTrue(bounded["evidence_truncated"])


if __name__ == "__main__":
    unittest.main()
