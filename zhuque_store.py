"""朱雀文本检测的本地存储层。

独立存储在 ``data/zhuque_text.db``，与 settings.json 解耦（沿用 later_read / todo 的约定）：
- ``records``：每次检测的历史记录（时间、原文、判定、原始返回、图表形式等）；
- ``settings``：API Key、合并段落、图表形式、上游地址等键值配置。

**API Key 不内置**，只能由用户自己在本工具里填写，保存在本机这个数据库里。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app_paths import get_data_dir
from zhuque_text import DEFAULT_CHART_MODE, DEFAULT_IS_MERGE, DEFAULT_UPSTREAM

logger = logging.getLogger(__name__)


class ZhuqueStoreError(RuntimeError):
    """存储层写失败（磁盘满、库损坏、被其它进程占用等）。

    读失败不抛这个异常：读的时候退回到安全默认值，保证界面还能开、还能用，
    只有"用户以为存下来了其实没存"的写操作才必须让界面知道。
    """


def _write_error(action: str, exc: BaseException) -> ZhuqueStoreError:
    logger.exception("朱雀历史存储 %s 失败: %s", action, exc)
    return ZhuqueStoreError("历史记录%s失败：%s" % (action, exc))


def _get_db_path() -> Path:
    return get_data_dir() / "zhuque_text.db"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    verdict    TEXT NOT NULL DEFAULT '',
    level      TEXT NOT NULL DEFAULT '',
    ai_score   REAL NOT NULL DEFAULT 0,
    ratio      REAL NOT NULL DEFAULT 0,
    human      REAL NOT NULL DEFAULT 0,
    is_merge   INTEGER NOT NULL DEFAULT 0,
    chart_mode TEXT NOT NULL DEFAULT 'svg',
    payload    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_zhuque_records_created ON records (created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# 老库升级用：pinned 是后加的列，缺了就补上（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
_MIGRATIONS = (
    ("pinned", "ALTER TABLE records ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"),
)


class ZhuqueStore:
    """朱雀检测历史 + 配置的 SQLite 存储。"""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _get_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self.broken = False
        self.recovered = False
        try:
            self._open()
            self._ensure_schema()
        except sqlite3.DatabaseError as exc:
            logger.exception("朱雀历史存储文件损坏，尝试备份并自动重建: %s", exc)
            self._conn = None
            if self._db_path.is_file():
                try:
                    import time
                    corrupted_backup = self._db_path.with_name(
                        f"{self._db_path.name}.corrupted.{int(time.time())}"
                    )
                    self._db_path.rename(corrupted_backup)
                    logger.info("旧损坏库已重命名备份为: %s", corrupted_backup)
                    self._open()
                    self._ensure_schema()
                    self.recovered = True
                except Exception as rec_exc:
                    logger.exception("自动重建失败: %s", rec_exc)
                    self._conn = None
                    self.broken = True
            else:
                self.broken = True
        except (sqlite3.Error, OSError) as exc:
            logger.exception("朱雀历史存储初始化失败，历史功能不可用: %s", exc)
            self._conn = None
            self.broken = True

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        """数据库文件位置（界面里用来告诉用户历史记录存在哪）。"""
        return Path(self._db_path)

    def _open(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.row_factory = sqlite3.Row
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

    def _ensure_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(_SCHEMA_SQL)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """缺列就补（老库直接加 pinned，不重建表）。"""
        assert self._conn is not None
        try:
            existing = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(records)").fetchall()
            }
        except (sqlite3.Error, KeyError, IndexError):
            return
        for column, statement in _MIGRATIONS:
            if column not in existing:
                try:
                    self._conn.execute(statement)
                except sqlite3.Error as exc:  # pragma: no cover - 老库结构异常
                    logger.warning("朱雀历史存储迁移 %s 失败: %s", column, exc)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
            self._ensure_schema()
        assert self._conn is not None
        return self._conn

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------
    def _get_setting(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
        return str(row["value"]) if row else default

    def _set_setting(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (str(key), value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)),
        )
        self._db.commit()

    def load_settings(self) -> dict[str, Any]:
        """读取工具配置；未设置过的项返回默认值（API Key 默认为空，不内置）。

        读失败时退回到默认值并记录日志，不让界面因为一个损坏的库就打不开。
        """
        try:
            key = self._get_setting("api_key", "")
            merge_raw = self._get_setting("is_merge", "true" if DEFAULT_IS_MERGE else "false")
            chart = self._get_setting("chart_mode", DEFAULT_CHART_MODE)
            upstream = self._get_setting("upstream", DEFAULT_UPSTREAM)
        except (sqlite3.Error, OSError) as exc:
            logger.exception("朱雀历史存储读取配置失败，回退到默认值: %s", exc)
            key, merge_raw, chart, upstream = "", "false", DEFAULT_CHART_MODE, DEFAULT_UPSTREAM
        is_merge = str(merge_raw).strip().lower() not in ("0", "false", "no", "")
        return {
            "api_key": key,
            "is_merge": bool(is_merge),
            "chart_mode": str(chart or DEFAULT_CHART_MODE),
            "upstream": str(upstream or DEFAULT_UPSTREAM),
        }

    def get_custom_setting(self, key: str, default: Any = None) -> Any:
        try:
            raw = self._get_setting(key, "")
            if not raw:
                return default
            try:
                return json.loads(raw)
            except Exception:
                return raw
        except (sqlite3.Error, OSError):
            return default

    def set_custom_setting(self, key: str, value: Any) -> None:
        try:
            self._set_setting(key, value)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("保存自定义配置 %s 失败: %s", key, exc)

    def save_settings(self, values: dict[str, Any]) -> None:
        try:
            self._save_settings(values)
        except (sqlite3.Error, OSError) as exc:
            raise _write_error("保存配置", exc) from exc

    def _save_settings(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            if k == "api_key":
                self._set_setting("api_key", str(v or ""))
            elif k == "is_merge":
                self._set_setting("is_merge", "true" if v else "false")
            elif k == "chart_mode":
                self._set_setting("chart_mode", str(v or DEFAULT_CHART_MODE))
            elif k == "upstream":
                url = str(v or "").strip() or DEFAULT_UPSTREAM
                self._set_setting("upstream", url)
            else:
                self._set_setting(k, v)

    # ------------------------------------------------------------------
    # 历史记录
    # ------------------------------------------------------------------
    def add_record(
        self,
        *,
        text: str,
        payload: dict[str, Any],
        summary: str = "",
        verdict: str = "",
        level: str = "",
        ai_score: float = 0.0,
        ratio: float = 0.0,
        human: float = 0.0,
        is_merge: bool = DEFAULT_IS_MERGE,
        chart_mode: str = DEFAULT_CHART_MODE,
        pinned: bool = False,
    ) -> dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": str(summary or ""),
            "text": str(text or ""),
            "verdict": str(verdict or ""),
            "level": str(level or ""),
            "ai_score": float(ai_score or 0.0),
            "ratio": float(ratio or 0.0),
            "human": float(human or 0.0),
            "is_merge": 1 if is_merge else 0,
            "chart_mode": str(chart_mode or DEFAULT_CHART_MODE),
            "pinned": 1 if pinned else 0,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
        }
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO records (id, created_at, summary, text, verdict, level, ai_score,"
                " ratio, human, is_merge, chart_mode, pinned, payload)"
                " VALUES (:id, :created_at, :summary, :text, :verdict, :level, :ai_score,"
                " :ratio, :human, :is_merge, :chart_mode, :pinned, :payload)",
                record,
            )
            self._db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise _write_error("写入", exc) from exc
        return record

    def list_records(self, limit: int = 200) -> list[dict[str, Any]]:
        """最新的在前；置顶的记录永远排在最前面。"""
        try:
            rows = self._db.execute(
                "SELECT id, created_at, summary, verdict, level, ai_score, ratio, human, is_merge,"
                " chart_mode, pinned, LENGTH(text) AS text_length FROM records"
                " ORDER BY pinned DESC, created_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            logger.exception("朱雀历史存储读取列表失败: %s", exc)
            return []
        return [dict(row) for row in rows]

    def set_pinned(self, record_id: str, pinned: bool) -> None:
        try:
            self._db.execute(
                "UPDATE records SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, str(record_id)),
            )
            self._db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise _write_error("置顶", exc) from exc

    def get_record(self, record_id: str) -> Optional[dict[str, Any]]:
        try:
            row = self._db.execute("SELECT * FROM records WHERE id = ?", (str(record_id),)).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.exception("朱雀历史存储读取单条失败: %s", exc)
            return None
        if row is None:
            return None
        record = dict(row)
        try:
            record["payload"] = json.loads(record.get("payload") or "{}")
        except json.JSONDecodeError:
            record["payload"] = {}
        return record

    def count_records(self) -> int:
        try:
            row = self._db.execute("SELECT COUNT(*) AS c FROM records").fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.exception("朱雀历史存储统计条数失败: %s", exc)
            return 0
        return int(row["c"]) if row else 0

    def delete_record(self, record_id: str) -> None:
        try:
            self._db.execute("DELETE FROM records WHERE id = ?", (str(record_id),))
            self._db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise _write_error("删除", exc) from exc

    def clear_records(self) -> int:
        count = self.count_records()
        try:
            self._db.execute("DELETE FROM records")
            self._db.commit()
            try:
                self._db.execute("VACUUM")
            except Exception:
                pass
        except (sqlite3.Error, OSError) as exc:
            raise _write_error("清空", exc) from exc
        return count
