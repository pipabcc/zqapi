from __future__ import annotations

import unittest
from zhuque_text import (
    build_report_html,
    json_to_html,
    mask_key,
    parse_report,
    pct,
    report_to_html_document,
    report_to_markdown,
    segment_ranges,
)


class TestZhuqueText(unittest.TestCase):
    def test_pct(self) -> None:
        self.assertEqual(pct(0.856), "85.6%")
        self.assertEqual(pct(0), "0.0%")
        self.assertEqual(pct(1), "100.0%")
        self.assertEqual(pct("invalid"), "未知")

    def test_mask_key(self) -> None:
        self.assertEqual(mask_key(""), "(未设置)")
        self.assertEqual(mask_key("12345"), "123**")
        long_key = "abcdef123456789xyz"
        masked = mask_key(long_key)
        self.assertIn("abcdef1", masked)
        self.assertIn("9xyz", masked)
        self.assertIn("共 18 位", masked)

    def test_parse_report(self) -> None:
        text = "第一段文本。第二段文本。"
        raw_result = {
            "status": "success",
            "ratio_confidence": 0.8,
            "softmax_confidence": 0.85,
            "labels_ratio": {"0": 0.2, "1": 0.8, "2": 0.0},
            "segment_labels": [
                {
                    "order": 1,
                    "label": 0,
                    "conf": 0.1,
                    "position": [0, 6],
                    "text": "第一段文本。",
                },
                {
                    "order": 2,
                    "label": 1,
                    "conf": 0.9,
                    "position": [6, 6],
                    "text": "第二段文本。",
                },
            ],
        }
        report = parse_report(raw_result, text)
        self.assertTrue(report.ok)
        self.assertEqual(report.verdict_level, "high")
        self.assertEqual(report.verdict_text, "很可能是 AI 生成")
        self.assertEqual(len(report.segments), 2)
        self.assertEqual(report.segments[0].name, "人工撰写")
        self.assertEqual(report.segments[1].name, "AI 生成")

        # 检查段落区间映射
        ranges = segment_ranges(report.segments, len(text))
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[1][1], len(text))

        # 检查导出
        md = report_to_markdown(report)
        self.assertIn("很可能是 AI 生成", md)
        self.assertIn("第一段文本", md)

        html_doc = report_to_html_document(report)
        self.assertIn("<!DOCTYPE html>", html_doc)
        self.assertIn("朱雀 AI 文本检测结果", html_doc)

    def test_json_to_html(self) -> None:
        data = {"code": 200, "message": "ok", "tags": ["ai", "test"]}
        html = json_to_html(data)
        self.assertIn('"code"', html)
        self.assertIn('"message"', html)
        self.assertIn('"ok"', html)


if __name__ == "__main__":
    unittest.main()
