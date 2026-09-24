from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zhuque_store import ZhuqueStore


class TestZhuqueStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_zhuque.db"
        self.store = ZhuqueStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_settings(self) -> None:
        settings = self.store.load_settings()
        self.assertEqual(settings.get("api_key"), "")

        self.store.save_settings({"api_key": "sk-test-123", "is_merge": True})
        loaded = self.store.load_settings()
        self.assertEqual(loaded.get("api_key"), "sk-test-123")
        self.assertTrue(loaded.get("is_merge"))

    def test_records_crud(self) -> None:
        self.assertEqual(self.store.count_records(), 0)

        record = self.store.add_record(
            text="测试文章正文内容",
            payload={"raw": 1},
            summary="测试摘要",
            verdict="很可能是人工撰写",
            level="low",
            ai_score=0.1,
            ratio=0.05,
            human=0.95,
        )
        rec_id = record["id"]
        self.assertEqual(self.store.count_records(), 1)

        fetched = self.store.get_record(rec_id)
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched["summary"], "测试摘要")
        self.assertEqual(fetched["payload"], {"raw": 1})

        # 置顶
        self.store.set_pinned(rec_id, True)
        listed = self.store.list_records()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["pinned"], 1)

        # 删除
        self.store.delete_record(rec_id)
        self.assertEqual(self.store.count_records(), 0)

    def test_clear_records(self) -> None:
        for i in range(3):
            self.store.add_record(text=f"text {i}", payload={})
        self.assertEqual(self.store.count_records(), 3)
        cleared = self.store.clear_records()
        self.assertEqual(cleared, 3)
        self.assertEqual(self.store.count_records(), 0)

    def test_custom_settings(self) -> None:
        self.assertIsNone(self.store.get_custom_setting("my_flag"))
        self.store.set_custom_setting("my_flag", True)
        self.assertTrue(self.store.get_custom_setting("my_flag"))
        self.store.set_custom_setting("my_dict", {"w": 800, "h": 600})
        self.assertEqual(self.store.get_custom_setting("my_dict"), {"w": 800, "h": 600})

    def test_corrupted_db_recovery(self) -> None:
        # 关闭当前库，并向文件写入非 SQLite 垃圾字节
        self.store.close()
        self.db_path.write_bytes(b"not a valid sqlite database format!!!")

        # 重新创建 Store，应自动触发备份并重建
        new_store = ZhuqueStore(self.db_path)
        self.assertTrue(new_store.recovered)
        self.assertFalse(new_store.broken)
        # 应能正常使用
        self.assertEqual(new_store.count_records(), 0)
        new_store.add_record(text="recovered", payload={})
        self.assertEqual(new_store.count_records(), 1)
        new_store.close()


if __name__ == "__main__":
    unittest.main()
