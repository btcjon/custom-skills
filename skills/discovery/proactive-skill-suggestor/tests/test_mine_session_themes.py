#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path

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
        self.assertNotIn("exactly", mod.title_tokens("Reply exactly lane B ready"))

    def test_cluster_keeps_single_heavy_shopify_session(self):
        sessions = [
            {
                "id": "a",
                "title": "Shopify access for trimsulin-revamp",
                "cwd": "/tmp/trimsulin-revamp",
                "tool_call_count": 40,
                "message_count": 20,
            },
            {"id": "b", "title": "Hello there", "tool_call_count": 1, "message_count": 2},
        ]
        skills = {"a": ["shopify-theme-operations"]}
        user_tokens = {"a": ["shopify", "theme", "preview"]}
        classes = mod.cluster_classes(sessions, skills, user_tokens, max_classes=8)
        labels = " ".join(c["label"] for c in classes)
        self.assertIn("shopify", labels)
        shop = next(c for c in classes if "shopify" in c["label"])
        self.assertTrue(any("shopify" in q for q in shop["hub_queries"]))
        self.assertIn(shop["qualify"], {"heavy", "recurring", "single"})

    def test_hub_queries_include_synonyms(self):
        qs = mod.hub_queries_for_class("shopify", ["shopify"], ["shopify-theme-operations"])
        self.assertIn("shopify", qs)
        self.assertTrue(any(q.endswith("api") or q.endswith("admin") for q in qs))

    def test_catalog(self):
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
