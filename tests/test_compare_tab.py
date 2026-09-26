from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from compare_history import CompareHistoryPanel
from compare_tab import TextCompareTab
import text_diff
from zhuque_store import ZhuqueStore


app = QApplication.instance() or QApplication([])


class TestCompareHistoryAndTab(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_zhuque.db"
        self.store = ZhuqueStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_compare_history_panel(self) -> None:
        panel = CompareHistoryPanel(self.store)
        self.assertEqual(panel.list_widget.count(), 0)

        # 插入高相似度记录
        self.store.add_compare_record(
            original_text="原文第1段内容",
            rewritten_text="原文第1段改写内容",
            similarity=0.92,
            diff_count=2,
            summary="原文第1段",
        )
        # 插入低相似度记录
        self.store.add_compare_record(
            original_text="完全不同的一篇文章",
            rewritten_text="彻底重写后的新内容",
            similarity=0.25,
            diff_count=10,
            summary="完全不同",
        )
        panel.refresh()
        self.assertEqual(panel.list_widget.count(), 2)

        # 筛选测试：高相似
        panel.sim_combo.setCurrentIndex(1)  # high
        visible_count = sum(1 for i in range(panel.list_widget.count()) if not panel.list_widget.item(i).isHidden())
        self.assertEqual(visible_count, 1)

        # 筛选测试：低相似
        panel.sim_combo.setCurrentIndex(3)  # low
        visible_count = sum(1 for i in range(panel.list_widget.count()) if not panel.list_widget.item(i).isHidden())
        self.assertEqual(visible_count, 1)

        # 还原全部
        panel.sim_combo.setCurrentIndex(0)
        visible_count = sum(1 for i in range(panel.list_widget.count()) if not panel.list_widget.item(i).isHidden())
        self.assertEqual(visible_count, 2)

        # 关键字筛选
        panel.search_edit.setText("第1段")
        visible_count = sum(1 for i in range(panel.list_widget.count()) if not panel.list_widget.item(i).isHidden())
        self.assertEqual(visible_count, 1)

        panel.close()

    def test_compare_tab_layout_and_history_integration(self) -> None:
        tab = TextCompareTab()
        tab._store = self.store
        tab._history_panel._store = self.store

        tab.resize(1380, 800)
        tab.show()
        app.processEvents()
        tab._balance_panels()
        app.processEvents()

        sizes = tab._splitter.sizes()
        self.assertEqual(len(sizes), 3)
        # 历史栏宽度为 224
        self.assertEqual(sizes[0], 224)
        # 原文与改写文平分剩余空间（允许整除 ±1 像素误差）
        self.assertAlmostEqual(sizes[1], sizes[2], delta=1)

        # 模拟点击「开始比较」并完成入库
        tab._original_edit.setPlainText("春眠不觉晓，处处闻啼鸟。")
        tab._rewritten_edit.setPlainText("春眠不觉晓，到处闻啼鸟。")
        tab._should_save_history = True
        res = text_diff.compare_texts("春眠不觉晓，处处闻啼鸟。", "春眠不觉晓，到处闻啼鸟。")
        tab._on_compare_finished(tab._request_seq, res)

        self.assertEqual(self.store.count_compare_records(), 1)
        records = self.store.list_compare_records()
        self.assertEqual(len(records), 1)
        rec_id = records[0]["id"]

        # 测试点击历史记录回填：平稳展示在对比结果视图，不切回输入模式，不闪烁按钮
        tab._on_history_record_selected(rec_id)
        self.assertEqual(tab._original_edit.toPlainText(), "春眠不觉晓，处处闻啼鸟。")
        self.assertEqual(tab._rewritten_edit.toPlainText(), "春眠不觉晓，到处闻啼鸟。")
        self.assertTrue(tab._result_tab_btn.isChecked(), "选择历史记录后应平滑保持在对比结果视图")
        self.assertFalse(tab._input_tab_btn.isChecked(), "选择历史记录不应跳动到输入按钮")
        self.assertEqual(tab._rewritten_stack.currentIndex(), 1, "改写文面板应保持在结果视图")

        # 测试切换「忽略大小写」不会增加历史记录
        count_before = self.store.count_compare_records()
        tab._ignore_case_check.setChecked(not tab._ignore_case_check.isChecked())
        tab._on_compare_finished(tab._request_seq, res)
        count_after = self.store.count_compare_records()
        self.assertEqual(count_before, count_after, "切换忽略大小写绝对不应增加历史记录")

        tab.cleanup()
        tab.close()


if __name__ == "__main__":
    unittest.main()
