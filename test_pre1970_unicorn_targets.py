from __future__ import annotations

import unittest

import pre1970_unicorn_targets as unicorns

# Curated radar invariants are deliberately strict because this file feeds live search.


class Pre1970UnicornTargetTests(unittest.TestCase):
    def test_curated_universe_has_exact_tier_counts_and_year_cutoff(self):
        rows = unicorns.load_targets()
        self.assertEqual(len(rows), 75)
        counts = {
            tier: sum(row["Unicorn tier"] == tier for row in rows)
            for tier in ("A", "B", "C")
        }
        self.assertEqual(counts, {"A": 25, "B": 25, "C": 25})
        self.assertTrue(all(int(row["Year"]) <= 1969 for row in rows))
        self.assertEqual(
            len({(row["Contributor"].casefold(), row["Title"].casefold()) for row in rows}),
            75,
        )

    def test_tier_a_queries_are_compact_and_searchable(self):
        rows = unicorns.tier_a_targets()
        self.assertEqual(len(rows), 25)
        queries = [unicorns.search_query(row) for row in rows]
        self.assertEqual(len(set(query.casefold() for query in queries)), 25)
        self.assertTrue(all(1 <= len(query) <= 100 for query in queries))
        self.assertTrue(all(unicorns.visible_terms(row) for row in rows))

    def test_landmark_targets_are_present(self):
        identities = {(row["Contributor"], row["Title"], row["Unicorn tier"]) for row in unicorns.load_targets()}
        for expected in {
            ("Bill Brandt", "A Night in London", "A"),
            ("Robert Frank", "Les Américains", "A"),
            ("Hiroshi Hamaya", "Yukiguni", "A"),
            ("Ken Domon", "Hiroshima", "A"),
            ("Kikuji Kawada", "The Map", "A"),
            ("Daido Moriyama", "Japan: A Photo Theater", "A"),
            ("Christer Strömholm", "Poste Restante", "A"),
            ("Danny Lyon", "The Bikeriders", "A"),
        }:
            self.assertIn(expected, identities)


if __name__ == "__main__":
    unittest.main()
