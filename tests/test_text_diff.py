# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from text_diff import (
    DELETE,
    EQUAL,
    INSERT,
    compare_texts,
    diff_to_html,
    diff_to_marked_text,
    diff_to_original_html,
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

    def test_delete_in_original_rendering(self) -> None:
        original = "春眠不觉晓，处处闻啼鸟。"
        rewritten = "春眠不觉晓，到处闻啼鸟！"
        res = compare_texts(original, rewritten)

        # 原文渲染：包含删除/修改前的文字，带下划线，不含新增文字
        orig_html = diff_to_original_html(res)
        self.assertIn("text-decoration:underline", orig_html)
        self.assertIn("#fee2e2", orig_html)
        self.assertIn("处", orig_html)
        self.assertNotIn("到", orig_html)  # 新增文字不应出现在原文

        # 改写文渲染（show_deletes=False）：包含新增文字，不含删除/删除线
        rewr_html = diff_to_html(res, show_deletes=False)
        self.assertIn("#dcfce7", rewr_html)
        self.assertIn("到", rewr_html)
        self.assertNotIn("line-through", rewr_html)

        # 标记文本（show_deletes=False）
        marked_no_del = diff_to_marked_text(res, show_deletes=False)
        self.assertIn("【+到+】", marked_no_del)
        self.assertNotIn("【-", marked_no_del)

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

    def test_long_text_blank_lines_alignment(self) -> None:
        """测试长文本在段落换行与空行数量不一致时，保持语义与词句精确对齐，不发生段落错位漂移。"""
        p1_orig = "前段时间，前理论物理学家 Matt von Hippel 在博客上给人工智能领域出了道考题。"
        p1_rewr = "前一段时间，曾经做过理论物理学家的 Matt von Hippel 在自己的博客上，给人工智能领域出了一道考题。"

        p2_orig = "乍听上去，容易以为人工智能推翻了什么物理定律。事情其实很具体，它没有新造物理学，但攻下了一个长年困扰学术界的重型计算。"
        p2_rewr = "乍听上去，容易以为人工智能推翻了什么物理定律。不过事情其实是很具体的，它并没有新造出什么物理学。"

        p3_orig = "所谓散射振幅，可以先退半步看它的用处。高能物理学家研究基本粒子，最核心的动作就是把粒子加速到极高能量撞在一起，测量飞散出来的碎片。"
        p3_rewr = "所谓散射振幅，可以先退半步看它的用处。高能物理学家在研究基本粒子的时候，最核心的那个动作，就是把粒子加速到极高的能量。"

        filler = "平面超对称杨米尔斯理论下的多圈散射振幅计算属于极度繁重的代数工程。" * 80

        # 原文标题后无空行，改写文标题后有空行
        orig = f"九圈散射振幅说明\n{p1_orig}\n\n{p2_orig}\n\n{p3_orig}\n\n{filler}"
        rewr = f"九圈散射振幅说明\n\n{p1_rewr}\n\n{p2_rewr}\n\n{p3_rewr}\n\n{filler}"

        res = compare_texts(orig, rewr)

        # 验证公共相同句子全部精准被识别为 EQUAL
        equal_texts = [r.old_text for r in res.runs if r.kind == EQUAL]
        self.assertTrue(any("九圈散射振幅说明" in t for t in equal_texts))
        self.assertTrue(any("乍听上去，容易以为人工智能推翻了什么物理定律。" in t for t in equal_texts))
        self.assertTrue(any("所谓散射振幅，可以先退半步看它的用处。" in t for t in equal_texts))


if __name__ == "__main__":
    unittest.main()
