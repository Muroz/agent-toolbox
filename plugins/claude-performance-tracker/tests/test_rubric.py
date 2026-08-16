"""rubric.yaml well-formedness.

The anchors are consumed only by the usage-evaluator subagent at runtime, so a
YAML typo there scores every run against an uncalibrated scale without a single
test failing. These assertions are the guard.

    python3 -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rubric  # noqa: E402


class TestRubricAnchors(unittest.TestCase):
    def test_every_dimension_has_a_full_0_1_2_scale(self):
        anchors = rubric.anchors()
        keys = rubric.dimension_keys()
        self.assertTrue(keys, "rubric declares no dimensions")
        self.assertEqual(set(anchors), keys,
                         "every declared dimension must appear in the anchor map")
        for key in sorted(keys):
            with self.subTest(dimension=key):
                self.assertEqual(set(anchors[key]), {"0", "1", "2"},
                                 f"{key} is missing an anchor level")
                for level, text in anchors[key].items():
                    self.assertTrue(text.strip(),
                                    f"{key} anchor {level} is empty")

    def test_version_is_stamped_and_numeric(self):
        v = rubric.version()
        self.assertNotEqual(v, "0", "rubric.yaml has no parseable version")
        self.assertTrue(v.isdigit(), f"unexpected rubric version {v!r}")

    def test_a_misspelled_anchors_key_is_caught(self):
        """The failure mode this suite exists for: `anchor:` instead of `anchors:`."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rubric.yaml"
            p.write_text(
                'version: "9"\n'
                "agent_behavior:\n"
                "  - key: good_dim\n"
                "    anchors:\n"
                '      "0": "bad"\n'
                '      "1": "ok"\n'
                '      "2": "great"\n'
                "  - key: typo_dim\n"
                "    anchor:\n"
                '      "0": "bad"\n')
            parsed = rubric.anchors(p)
            self.assertEqual(set(parsed["good_dim"]), {"0", "1", "2"})
            self.assertEqual(parsed["typo_dim"], {},
                             "a misspelled anchors key must not parse as anchors")


if __name__ == "__main__":
    unittest.main()
