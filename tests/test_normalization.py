"""Unit tests for the configurable document normalization pipeline."""

import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from services import build_structured_content, run_normalization_pipeline


COMPOSITE_DOCUMENT = """# Guide\r
<p>Le document est un guide pour les utilisateurs et les équipes.</p>\r
\r
| Nom | Valeur |\r
|:----|------:|\r
| alpha | 10 |\r
\r
- [x] Première étape\r
  - Sous-étape\r
1. Deuxième étape\r
\r
```python\r
def hello():\r
    return "<ok>"\r
```\r
\r
Voir [documentation](https://example.test/doc) et ![schéma](image.png).\r
Référence[^1].\r
[^1]: Note utile.\r
"""


class NormalizationPipelineTest(unittest.TestCase):
    def test_composite_markdown_is_preserved_and_extracted(self):
        result = run_normalization_pipeline(COMPOSITE_DOCUMENT)
        metadata = result["extracted_metadata"]

        self.assertEqual(result["raw_content"], COMPOSITE_DOCUMENT)
        self.assertNotIn("\r", result["normalized_content"])
        self.assertIn('return "<ok>"', result["normalized_content"])
        self.assertEqual(metadata["tables"][0]["headers"], ["Nom", "Valeur"])
        self.assertEqual(metadata["tables"][0]["rows"], [["alpha", "10"]])
        self.assertEqual(len(metadata["lists"]), 3)
        self.assertTrue(metadata["lists"][0]["checked"])
        self.assertEqual(metadata["code_blocks"][0]["language"], "python")
        self.assertEqual(metadata["links"][0]["url"], "https://example.test/doc")
        self.assertEqual(metadata["images"][0]["url"], "image.png")
        self.assertEqual(metadata["notes"][0], {"id": "1", "content": "Note utile."})
        self.assertEqual(result["detected_language"], "fr")
        self.assertEqual(result["content_type"], "mixed")

        structured = build_structured_content(
            result["normalized_content"],
            heading_max_level=6,
            extracted_metadata=metadata,
        )
        self.assertEqual(structured["section_count"], 1)
        self.assertEqual(structured["elements"]["tables"][0]["row_count"], 1)

    def test_stages_can_be_disabled(self):
        result = run_normalization_pipeline(
            "<b>Texte</b>",
            {
                "enabled_stages": ["line_endings"],
                "preserve_code_blocks": False,
            },
        )
        self.assertIn("<b>Texte</b>", result["normalized_content"])
        self.assertEqual(result["detected_language"], "und")
        self.assertEqual(result["content_type"], "unknown")

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(ValueError):
            run_normalization_pipeline("texte", {"enabled_stages": ["unknown"]})


if __name__ == "__main__":
    unittest.main()
