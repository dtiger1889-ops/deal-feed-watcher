"""Offline tests: price extraction, categorizing, scoring, and one full run
against a local RSS file. Every title here is invented.

Run from the repo folder:  python -m unittest discover -s tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deal_watcher as w  # noqa: E402

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Sample deals</title>
<item><guid>a1</guid><title>Example Brand 2TB NVMe SSD for $89 (was $149)</title>
<link>https://deals.example/a1</link><description>Free shipping over $35</description>
<pubDate>Sat, 17 Oct 2026 12:00:00 GMT</pubDate></item>
<item><guid>a2</guid><title>Sample Co. Camping Tent 4-Person for $60</title>
<link>https://deals.example/a2</link><description></description></item>
<item><guid>a3</guid><title>Plush Toy Bundle 30% off</title>
<link>https://deals.example/a3</link><description></description></item>
</channel></rss>
"""

WATCHLIST = {
    "entries": [
        {"id": "big-ssd", "tier": "hot", "patterns": ["NVMe|SSD"],
         "require_patterns": ["\\b[24]TB\\b"], "price_max_usd": 120},
        {"id": "tent", "tier": "watch", "patterns": ["\\btent\\b"]},
    ],
    "never_show": ["plush"],
    "hidden_for_now": [],
}


class Pricing(unittest.TestCase):
    def test_sale_and_original(self):
        self.assertEqual(w.extract_pricing("Widget for $40 (was $80)"), (40.0, 80.0, 50))

    def test_shipping_charge_is_not_the_price(self):
        sale, _, _ = w.extract_pricing("Gadget sale", "now $25, shipping $5")
        self.assertEqual(sale, 25.0)

    def test_percent_only(self):
        self.assertEqual(w.extract_pricing("Everything 30% off")[2], 30)


class Categories(unittest.TestCase):
    def test_title_only(self):
        self.assertEqual(w.categorize("Example 1TB SSD"), "computers")

    def test_anti_word(self):
        self.assertNotEqual(w.categorize("Cartoon Minnie Mouse Plush"), "electronics")

    def test_pack_sizes_split_groups(self):
        self.assertNotEqual(w.product_key("Sample Batteries 4-pack", "s"),
                            w.product_key("Sample Batteries 12-pack", "s"))


class FullRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        feed = self.tmp / "feed.xml"
        feed.write_text(FEED, encoding="utf-8")
        (self.tmp / "sources.json").write_text(json.dumps(
            {"sources": [{"name": "Sample", "type": "rss", "url": feed.as_uri()}]}), encoding="utf-8")
        (self.tmp / "watchlist.json").write_text(json.dumps(WATCHLIST), encoding="utf-8")
        for name, rel in [("DB_FILE", "deals.db"), ("SOURCES_FILE", "sources.json"),
                          ("WATCHLIST_FILE", "watchlist.json"), ("DASHBOARD_FILE", "dashboard.html"),
                          ("DIGEST_FILE", "digest_feed.json"), ("LOG_FILE", "watcher.log")]:
            setattr(w, name, self.tmp / rel)

    def test_run_scores_and_renders(self):
        w.run()
        digest = json.loads((self.tmp / "digest_feed.json").read_text(encoding="utf-8"))
        by_id = {m["guid"]: m for m in digest["matches"]}
        self.assertEqual(by_id["a1"]["tier"], "hot")
        self.assertEqual(by_id["a1"]["price"], 89.0)
        self.assertEqual(by_id["a2"]["tier"], "watch")
        self.assertNotIn("a3", by_id)  # never_show
        page = (self.tmp / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Example Brand 2TB NVMe SSD", page)
        self.assertNotIn("Plush Toy Bundle", page)


if __name__ == "__main__":
    unittest.main()
