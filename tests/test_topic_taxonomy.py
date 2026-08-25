from __future__ import annotations

import unittest

from tools.acm_agent.topic_taxonomy import (
    TAXONOMY_VERSION,
    TOPIC_LABELS,
    classify_tags,
)


class TopicTaxonomyTests(unittest.TestCase):
    def test_current_codeforces_official_tags_are_all_accounted_for(self) -> None:
        official = [
            "2-sat", "binary search", "bitmasks", "brute force",
            "chinese remainder theorem", "combinatorics",
            "constructive algorithms", "data structures", "dfs and similar",
            "divide and conquer", "dp", "dsu", "expression parsing", "fft",
            "flows", "games", "geometry", "graph matchings", "graphs",
            "greedy", "hashing", "implementation", "interactive", "math",
            "matrices", "meet-in-the-middle", "number theory", "probabilities",
            "schedules", "shortest paths", "sortings", "string suffix structures",
            "strings", "ternary search", "trees", "two pointers",
        ]
        result = classify_tags(official)
        self.assertEqual(result.unclassified, ())

    def test_cross_platform_aliases_share_stable_topics(self) -> None:
        english = classify_tags(["dp", "flows", "segment tree"])
        chinese = classify_tags(["动态规划", "网络流", "线段树"])
        self.assertEqual(english.topics, chinese.topics)
        self.assertEqual(
            set(english.topics),
            {"dynamic_programming", "matching_flows", "range_data_structures"},
        )

    def test_metadata_and_generic_tags_are_ignored(self) -> None:
        result = classify_tags(["2026", "北京", "O2优化", "算法", "模板题"])
        self.assertEqual(result.topics, ())
        self.assertEqual(result.unclassified, ())

    def test_unknown_subject_tag_is_reported_without_model_inference(self) -> None:
        result = classify_tags(["brand new algorithm family"])
        self.assertEqual(result.topics, ())
        self.assertEqual(result.unclassified, ("brand new algorithm family",))

    def test_taxonomy_is_versioned_and_labels_are_complete(self) -> None:
        self.assertEqual(TAXONOMY_VERSION, "1")
        self.assertEqual(TOPIC_LABELS["dynamic_programming"], "动态规划")
        self.assertTrue(all(key and label for key, label in TOPIC_LABELS.items()))


if __name__ == "__main__":
    unittest.main()
