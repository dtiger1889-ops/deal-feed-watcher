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

    def test_plain_couch_is_home(self):
        self.assertEqual(w.categorize("Example couch for $300"), "home")


class TempFiles(unittest.TestCase):
    """Points every file the watcher writes at a throwaway folder."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, rel in [("DB_FILE", "deals.db"), ("LOG_FILE", "watcher.log"),
                          ("PRICE_HISTORY_FILE", "price_history.json")]:
            setattr(w, name, self.tmp / rel)


class Repricing(TempFiles):
    def test_repriced_offer_scored_against_updated_group(self):
        db = w.open_db()
        title = "Sample Desk Lamp Brass Finish"
        pk = w.product_key(title, "s")
        for guid, price in [("g1", 50.0), ("g2", 50.0), ("g3", 100.0), ("g4", 100.0)]:
            db.execute("INSERT INTO deals (guid, title, price, product_key) VALUES (?,?,?,?)",
                       (guid, title, price, pk))
        w.load_baseline(db)
        w.refresh_baseline_group(db, pk, "2026-01-01")
        item = {"guid": "g4", "title": title, "description": "now $60", "source": "s"}
        lamp = {"entries": [{"id": "lamp", "tier": "hot", "patterns": ["\\blamp\\b"],
                             "min_discount_pct": 15}],
                "never_show": [], "hidden_for_now": []}
        w.WATCHLIST_FILE = self.tmp / "watchlist.json"
        w.WATCHLIST_FILE.write_text(json.dumps(lamp), encoding="utf-8")
        w.refresh_existing(db, item, w.load_watchlist(), "2026-01-02T00:00:00")
        median, tier = db.execute(
            "SELECT baseline_median, tier FROM deals WHERE guid='g4'").fetchone()
        self.assertEqual(median, 55.0)  # median of 50, 50, 60, 100 -- not the stale 75
        # $60 is 20% under the stale 75 but above the updated 55: not hot.
        self.assertEqual(tier, "watch")
        db.close()


class Consolidate(TempFiles):
    def test_folded_month_is_not_overwritten(self):
        saved = {"2020-01": {"ssd": {"2TB": {"deals": 9, "min_price": 70.0,
                                             "median_price": 90.0, "avg_discount_pct": 20}}}}
        w.PRICE_HISTORY_FILE.write_text(json.dumps(saved), encoding="utf-8")
        db = w.open_db()
        db.execute("INSERT INTO price_archive (seen_date, part_type, spec, title, price) "
                   "VALUES ('2020-01-15', 'ssd', '2TB', 'Sample SSD', 300.0)")
        db.commit()
        db.close()
        w.consolidate()
        history = json.loads(w.PRICE_HISTORY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(history, saved)


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
