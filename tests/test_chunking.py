"""Unit tests for structure-aware chunking and RAG lineage metadata."""

import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services import (
    build_chunks_for_document,
    build_summary_short,
    estimate_tokens,
    extract_chunk_zones,
    parse_chunk_options,
    split_code_zone,
    split_markdown_sections,
    split_section_content_aware,
    split_table_zone,
    split_text_zone,
)


COMPOSITE_DOCUMENT = """# Guide
Prose Alpha courte.

Prose Beta minuscule.

| Nom | Valeur |
| --- | --- |
| alpha | un deux trois |
| beta | quatre cinq six |
| gamma | sept huit neuf |

```python
def alpha():
    return "un deux trois"
def beta():
    return "quatre cinq six"
def gamma():
    return "sept huit neuf"
```

<!-- chunk:strict:start enveloppe -->
STRICT_PAYLOAD alpha beta gamma
<!-- chunk:strict:end -->

Prose Omega finale.
"""


def make_options(**overrides):
    """Return validated deterministic options suitable for small fixtures."""
    payload = {
        "chunkMaxTokens": 16,
        "chunkOverlapTokens": 0,
        "hardMaxTokens": 18,
        "headingMaxLevel": 6,
        "tokenEstimator": "words",
        "smallParagraphMinTokens": 5,
    }
    payload.update(overrides)
    return parse_chunk_options(payload)


def make_document(content, document_id="document-test"):
    """Build the minimum document payload accepted by the chunk builder."""
    return {
        "uuid": document_id,
        "title_document": "Guide",
        "content_document": content,
    }


class StructureAwareChunkingTest(unittest.TestCase):
    def test_composite_document_preserves_zones_and_rag_metadata(self):
        options = make_options()
        chunks = build_chunks_for_document(
            make_document(COMPOSITE_DOCUMENT),
            previous_document_id="document-precedent",
            options=options,
        )

        self.assertGreater(len(chunks), 4)
        chunk_types = {chunk["metadata"]["chunk_type"] for chunk in chunks}
        self.assertEqual(chunk_types, {"markdown", "table", "code", "strict"})

        table_chunks = [
            chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "table"
        ]
        code_chunks = [
            chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "code"
        ]
        strict_chunks = [
            chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "strict"
        ]
        prose_chunks = [
            chunk for chunk in chunks if chunk["metadata"]["chunk_type"] == "markdown"
        ]

        self.assertGreater(len(table_chunks), 1)
        self.assertGreater(len(code_chunks), 1)
        self.assertEqual(len(strict_chunks), 1)

        expected_header = ["| Nom | Valeur |", "| --- | --- |"]
        expected_rows = [
            "| alpha | un deux trois |",
            "| beta | quatre cinq six |",
            "| gamma | sept huit neuf |",
        ]
        recovered_rows = []
        for chunk in table_chunks:
            lines = chunk["text"].splitlines()
            self.assertEqual(lines[:2], expected_header)
            recovered_rows.extend(lines[2:])
            self.assertEqual(chunk["metadata"]["zone_type"], "table")
            self.assertTrue(chunk["metadata"]["strict_zone"])
        self.assertEqual(recovered_rows, expected_rows)

        expected_code_lines = [
            "def alpha():",
            '    return "un deux trois"',
            "def beta():",
            '    return "quatre cinq six"',
            "def gamma():",
            '    return "sept huit neuf"',
        ]
        recovered_code_lines = []
        for chunk in code_chunks:
            lines = chunk["text"].splitlines()
            self.assertEqual(lines[0], "```python")
            self.assertEqual(lines[-1], "```")
            recovered_code_lines.extend(lines[1:-1])
            self.assertEqual(chunk["metadata"]["zone_type"], "code")
            self.assertTrue(chunk["metadata"]["strict_zone"])
        self.assertEqual(recovered_code_lines, expected_code_lines)

        self.assertEqual(
            strict_chunks[0]["text"],
            "STRICT_PAYLOAD alpha beta gamma",
        )
        self.assertEqual(strict_chunks[0]["metadata"]["zone_type"], "strict")
        self.assertTrue(strict_chunks[0]["metadata"]["strict_zone"])

        for chunk in prose_chunks:
            self.assertNotIn("| Nom | Valeur |", chunk["text"])
            self.assertNotIn("```python", chunk["text"])
            self.assertNotIn("STRICT_PAYLOAD", chunk["text"])
            self.assertEqual(chunk["metadata"]["zone_type"], "text")
            self.assertFalse(chunk["metadata"]["strict_zone"])

        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        expected_ratios = [
            round(index / float(len(chunks) - 1), 5)
            for index in range(len(chunks))
        ]
        for index, chunk in enumerate(chunks):
            metadata = chunk["metadata"]
            self.assertEqual(chunk["pageContent"], chunk["text"])
            self.assertLessEqual(
                estimate_tokens(chunk["text"], options),
                options["hardMaxTokens"],
            )
            self.assertTrue(metadata["summary_short"])
            self.assertLessEqual(len(metadata["summary_short"]), 240)
            self.assertEqual(metadata["document_id"], "document-test")
            self.assertEqual(
                metadata["previous_document_id"],
                "document-precedent",
            )
            self.assertEqual(
                metadata["previous_chunk_id"],
                chunk_ids[index - 1] if index else None,
            )
            self.assertEqual(
                metadata["next_chunk_id"],
                chunk_ids[index + 1] if index + 1 < len(chunks) else None,
            )
            self.assertEqual(
                metadata["document_position_ratio"],
                expected_ratios[index],
            )

    def test_single_chunk_has_zero_ratio_and_empty_chunk_links(self):
        chunks = build_chunks_for_document(
            make_document("# Solo\nUn contenu suffisamment court."),
            previous_document_id=None,
            options=make_options(chunkMaxTokens=50, hardMaxTokens=50),
        )

        self.assertEqual(len(chunks), 1)
        metadata = chunks[0]["metadata"]
        self.assertEqual(metadata["document_position_ratio"], 0.0)
        self.assertIsNone(metadata["previous_chunk_id"])
        self.assertIsNone(metadata["next_chunk_id"])

    def test_repeated_heading_never_exceeds_the_hard_limit(self):
        options = make_options(
            chunkMaxTokens=8,
            hardMaxTokens=8,
            chunkOverlapTokens=1,
        )
        chunks = build_chunks_for_document(
            make_document(
                "# Section title very long\n"
                "one two three four five six seven eight nine ten eleven twelve"
            ),
            previous_document_id=None,
            options=options,
        )

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(
                estimate_tokens(chunk["text"], options),
                options["hardMaxTokens"],
            )

    def test_summary_short_is_truncated_to_the_rag_preview_limit(self):
        source = " ".join(f"mot-{index:03d}" for index in range(80))

        summary = build_summary_short(source)

        self.assertEqual(len(summary), 240)
        self.assertTrue(summary.endswith("…"))

    def test_markdown_headings_inside_longer_fence_are_not_sections(self):
        content = """# Avant
Texte avant.

````python
# Faux titre
```not-close
## Toujours du code
print("ok")
`````

# Apres
Texte apres.
"""

        sections = split_markdown_sections(content, heading_max_level=6)

        self.assertEqual(
            [section["section_title"] for section in sections],
            ["Avant", "Apres"],
        )
        self.assertIn("# Faux titre", sections[0]["content"])
        self.assertIn("```not-close", sections[0]["content"])
        self.assertIn("## Toujours du code", sections[0]["content"])
        self.assertIn("`````", sections[0]["content"])

        code_zones = [
            zone
            for zone in extract_chunk_zones(sections[0]["content"])
            if zone["zone_type"] == "code"
        ]
        self.assertEqual(len(code_zones), 1)
        self.assertTrue(code_zones[0]["content"].startswith("````python\n"))
        self.assertTrue(code_zones[0]["content"].endswith("\n`````"))

    def test_code_is_split_only_between_lines_and_oversized_line_is_rejected(self):
        options = make_options(chunkMaxTokens=7, hardMaxTokens=9)
        source = """```python
alpha beta
gamma delta
epsilon zeta
eta theta
```"""
        expected_lines = source.splitlines()[1:-1]

        parts = split_code_zone(source, options)

        self.assertGreater(len(parts), 1)
        recovered_lines = []
        for part in parts:
            lines = part.splitlines()
            self.assertEqual(lines[0], "```python")
            self.assertEqual(lines[-1], "```")
            recovered_lines.extend(lines[1:-1])
            self.assertLessEqual(
                estimate_tokens(part, options),
                options["hardMaxTokens"],
            )
        self.assertEqual(recovered_lines, expected_lines)

        oversized = """```python
alpha beta gamma delta epsilon zeta eta theta
```"""
        original = oversized
        with self.assertRaisesRegex(ValueError, "ligne de code"):
            split_code_zone(oversized, options)
        self.assertEqual(oversized, original)
        with self.assertRaisesRegex(ValueError, "Bloc de code non ferme"):
            extract_chunk_zones("```python\nprint('incomplet')")

    def test_table_is_split_only_between_rows_and_oversized_row_is_rejected(self):
        options = make_options(chunkMaxTokens=15, hardMaxTokens=18)
        source = """| Nom | Valeur |
| --- | --- |
| alpha | un deux trois |
| beta | quatre cinq six |"""
        header = source.splitlines()[:2]
        expected_rows = source.splitlines()[2:]

        parts = split_table_zone(source, options)

        self.assertGreater(len(parts), 1)
        recovered_rows = []
        for part in parts:
            lines = part.splitlines()
            self.assertEqual(lines[:2], header)
            recovered_rows.extend(lines[2:])
            self.assertLessEqual(
                estimate_tokens(part, options),
                options["hardMaxTokens"],
            )
        self.assertEqual(recovered_rows, expected_rows)

        oversized = """| Nom | Valeur |
| --- | --- |
| alpha | un deux trois quatre cinq six sept huit neuf dix |"""
        original = oversized
        with self.assertRaisesRegex(ValueError, "ligne de tableau"):
            split_table_zone(oversized, options)
        self.assertEqual(oversized, original)

        oversized_header = """| Nom alpha beta gamma delta epsilon zeta eta theta | Valeur encore plus longue |
| --- | --- |"""
        with self.assertRaisesRegex(ValueError, "en-tete du tableau"):
            split_table_zone(oversized_header, options)

    def test_unclosed_and_oversized_strict_zones_are_rejected(self):
        unclosed = """Texte.
<!-- chunk:strict:start contrat -->
contenu sans fin"""
        with self.assertRaisesRegex(ValueError, "Zone stricte non fermee"):
            extract_chunk_zones(unclosed)

        oversized = """<!-- chunk:strict:start contrat -->
un deux trois quatre cinq six sept huit neuf dix onze
<!-- chunk:strict:end -->"""
        options = make_options(chunkMaxTokens=8, hardMaxTokens=10)
        with self.assertRaisesRegex(ValueError, "depasse hardMaxTokens"):
            split_section_content_aware(oversized, options)

    def test_small_paragraph_merging_is_configurable(self):
        content = "Alpha bref.\n\nBeta bref.\n\nGamma bref."
        merged_options = make_options(
            chunkMaxTokens=20,
            hardMaxTokens=20,
            mergeSmallParagraphs=True,
        )
        separate_options = make_options(
            chunkMaxTokens=20,
            hardMaxTokens=20,
            mergeSmallParagraphs=False,
        )

        merged = split_text_zone(content, merged_options)
        separate = split_text_zone(content, separate_options)

        self.assertEqual(merged, [content])
        self.assertEqual(
            separate,
            ["Alpha bref.", "Beta bref.", "Gamma bref."],
        )
        self.assertLess(len(merged), len(separate))

    def test_chunk_option_parser_validates_strict_types_and_numeric_bounds(self):
        options = parse_chunk_options(
            {
                "chunkMaxTokens": 8,
                "chunkOverlapTokens": 99,
                "hardMaxTokens": 2,
                "headingMaxLevel": 99,
                "charsPerToken": 0,
                "smallParagraphMinTokens": 999,
                "tokenEstimator": "inconnu",
                "codeAware": "false",
                "tableAware": "true",
                "mergeSmallParagraphs": "0",
                "strictZoneTypes": " code, table, code, strict ",
            }
        )

        self.assertEqual(options["hardMaxTokens"], 8)
        self.assertEqual(options["chunkOverlapTokens"], 7)
        self.assertEqual(options["headingMaxLevel"], 6)
        self.assertEqual(options["charsPerToken"], 1)
        self.assertEqual(options["smallParagraphMinTokens"], 8)
        self.assertEqual(options["tokenEstimator"], "words")
        self.assertFalse(options["codeAware"])
        self.assertTrue(options["tableAware"])
        self.assertFalse(options["mergeSmallParagraphs"])
        self.assertEqual(
            options["strictZoneTypes"],
            ["code", "table", "strict"],
        )

        with self.assertRaisesRegex(ValueError, "Type de zone stricte inconnu"):
            parse_chunk_options({"strictZoneTypes": ["code", "archive"]})
        with self.assertRaisesRegex(ValueError, "doit etre une liste"):
            parse_chunk_options({"strictZoneTypes": 42})


if __name__ == "__main__":
    unittest.main()
