#!/usr/bin/env python3
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mine_session_themes", ROOT / "scripts" / "mine_session_themes.py"
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(mod)


class MineTests(unittest.TestCase):
    def test_title_tokens_drop_stopwords(self):
        toks = mod.title_tokens("Please help with Stripe webhook triage")
        self.assertIn("stripe", toks)
        self.assertIn("webhook", toks)
        self.assertNotIn("please", toks)

    def test_cluster_and_catalog(self):
        sessions = [
            {"id": "a", "title": "Stripe webhook triage"},
            {"id": "b", "title": "Stripe webhook retry"},
            {"id": "c", "title": "Hello there"},
        ]
        skills = {"a": ["stripe-webhook-triage"], "b": ["stripe-webhook-triage"]}
        classes = mod.cluster_classes(sessions, skills, max_classes=5)
        self.assertGreaterEqual(len(classes), 1)
        labels = " ".join(c["label"].lower() for c in classes)
        self.assertIn("stripe", labels)

        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "stripe-webhook-triage"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: stripe-webhook-triage\n"
                "description: Use when Stripe webhooks fail.\n---\n\n# x\n",
                encoding="utf-8",
            )
            cat = mod.local_catalog([Path(td)])
            self.assertEqual(cat[0]["name"], "stripe-webhook-triage")


if __name__ == "__main__":
    unittest.main()
