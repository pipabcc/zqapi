# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from text_diff import (
    DELETE,
    EQUAL,
    INSERT,
    MODIFY,
    compare_texts,
    diff_to_marked_text,
    diff_to_rewritten_text,
    format_stats,
    normalize_whitespace,
    tokenize,
)


class TestTextDiff(unittest.TestCase):
    def test_tokenize(self) -> None:
        tokens = tokenize("Hello 世界 123! \n")
        self.assertIn("Hello", tokens)
        self.assertIn("世", tokens)
        self.assertIn("界", tokens)
        self.assertIn("123", tokens)
        self.assertIn(" ", tokens)

    def test_normalize_whitespace(self) -> None:
        text = "  hello   world \n\n\n\t test  "
        self.assertEqual(normalize_whitespace(text), "hello world\n\ntest")

    def test_identical_texts(self) -> None:
        text = "这是一篇完全相同的文章。"
        res = compare_texts(text, text)
        self.assertTrue(res.stats.is_identical)
        self.assertEqual(res.stats.similarity, 1.0)
        self.assertEqual(len(res.runs), 1)
        self.assertEqual(res.runs[0].kind, EQUAL)
        self.assertIn("完全一致", format_stats(res.stats))

    def test_simple_diff(self) -> None:
        original = "春眠不觉晓，处处闻啼鸟。"
        rewritten = "春眠不觉晓，到处闻啼鸟！"
        res = compare_texts(original, rewritten)
        self.assertFalse(res.stats.is_identical)
        self.assertEqual(diff_to_rewritten_text(res), rewritten)
        marked = diff_to_marked_text(res)
        self.assertIn("【-处-】【+到+】处", marked)
        self.assertIn("【-。-】【+\uff01+】", marked)

    def test_ignore_options(self) -> None:
        original = "Hello   World"
        rewritten = "hello world"
        # 默认区分
        res1 = compare_texts(original, rewritten)
        self.assertFalse(res1.stats.is_identical)

        # 忽略大小写与空白
        res2 = compare_texts(original, rewritten, ignore_whitespace=True, ignore_case=True)
        self.assertTrue(res2.stats.is_identical)

    def test_empty_inputs(self) -> None:
        res1 = compare_texts("", "")
        self.assertTrue(res1.stats.is_identical)

        res2 = compare_texts("hello", "")
        self.assertEqual(len(res2.runs), 1)
        self.assertEqual(res2.runs[0].kind, DELETE)

        res3 = compare_texts("", "world")
        self.assertEqual(len(res3.runs), 1)
        self.assertEqual(res3.runs[0].kind, INSERT)


if __name__ == "__main__":
    unittest.main()
