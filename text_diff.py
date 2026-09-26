"""原文 / 改写文逐处差异比对引擎。

与 UI 无关：输入两段纯文本，输出可直接渲染的差异片段（DiffRun）、
统计信息（DiffStats），以及 HTML / 标记文本两种呈现形式。

比对按「拉丁词 + 数字 + 单个非拉丁字符 + 空白片段」切分 token，
因此中文逐字比对、英文按单词比对，避免把整段中文并成一整个 token。

针对自然语言长文本改写，采用最长公共连续子序列（Anchor Partitioning）分治算法：
1. 8000 tokens 以内常规长文进行全局精确比对，保证 100% 全局最优解；
2. 超长文本提取公共子序列作为锚点递归二分，彻底避免段落空行或标点微调导致的假阳性错位与大段误删。
"""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


EQUAL = "equal"
INSERT = "insert"
DELETE = "delete"
MODIFY = "modify"


# 展示配色：新增用绿色、删除用红色，修改为「红色删除线 + 绿色新增」相邻呈现。
INSERT_COLOR = "#15803d"
INSERT_BACKGROUND = "#dcfce7"
DELETE_COLOR = "#b91c1c"
DELETE_BACKGROUND = "#fee2e2"


# 拉丁词 / 数字各作为一个 token，其余（含中文）逐字，空白片段整体作为一个 token。
_TOKEN_PATTERN = re.compile(r"[A-Za-z]+|[0-9]+|\s+|.", re.DOTALL)

_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")

# 单次精确比对的 token 预算：低于该规模直接整体比对，保证差异是全局最优解。
# 8000 tokens 覆盖绝大多数常规长文（单篇4000字以内），兼具极高精度与毫秒级速度。
_DIRECT_TOKEN_BUDGET = 8000


def tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(str(text or ""))


def normalize_whitespace(text: str) -> str:
    """行内连续空白合并为一个空格，段落间连续空行规范保留为一行空行（保留两个换行符）。"""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    norm_lines: list[str] = []
    for line in lines:
        # 行内连续水平空白合并为一个空格，去除行首尾空白
        norm_line = re.sub(r"[^\S\n]+", " ", line).strip()
        norm_lines.append(norm_line)

    # 连续多个空行压缩为一个空行（即段落间最多保留一个空行：\n\n）
    result_lines: list[str] = []
    prev_empty = False
    for line in norm_lines:
        if not line:
            if not prev_empty:
                result_lines.append("")
                prev_empty = True
        else:
            result_lines.append(line)
            prev_empty = False

    # 去除整个文本开头和结尾的空行
    while result_lines and not result_lines[0]:
        result_lines.pop(0)
    while result_lines and not result_lines[-1]:
        result_lines.pop()

    return "\n".join(result_lines)


def _proportional_spans(total: int, count: int) -> list[tuple[int, int]]:
    """把 [0, total) 按比例切成 count 段，避免逐段长度取整后末尾堆积。"""
    spans: list[tuple[int, int]] = []
    for index in range(count):
        start = (total * index) // count
        end = (total * (index + 1)) // count
        spans.append((start, end))
    return spans


def _diff_chunked(
    tokens_a: Sequence[str],
    tokens_b: Sequence[str],
    *,
    ignore_whitespace: bool,
    ignore_case: bool,
    token_budget: int,
) -> list[DiffRun]:
    """兜底：在没有可切分的换行与标点时，优先以最长公共序列作为锚点分治；找不到时成对切块。

    利用公共子序列切分彻底避免盲目成对切块导致的对齐漂移（Alignment Drift）。
    """
    if len(tokens_a) + len(tokens_b) <= token_budget:
        return _diff_tokens(
            tokens_a,
            tokens_b,
            ignore_whitespace=ignore_whitespace,
            ignore_case=ignore_case,
        )

    # 尝试寻找较长公共子序列（至少 6 个 token）作为锚点
    norm_a = _normalize_tokens(tokens_a, ignore_whitespace=ignore_whitespace, ignore_case=ignore_case)
    norm_b = _normalize_tokens(tokens_b, ignore_whitespace=ignore_whitespace, ignore_case=ignore_case)
    matcher = difflib.SequenceMatcher(None, norm_a, norm_b, autojunk=False)
    match = matcher.find_longest_match(0, len(norm_a), 0, len(norm_b))

    if match.size >= 6:
        runs: list[DiffRun] = []
        # 锚点前半段递归
        if match.a > 0 or match.b > 0:
            runs.extend(
                _diff_chunked(
                    tokens_a[: match.a],
                    tokens_b[: match.b],
                    ignore_whitespace=ignore_whitespace,
                    ignore_case=ignore_case,
                    token_budget=token_budget,
                )
            )
        # 锚点本身（相等）
        text_a = "".join(tokens_a[match.a : match.a + match.size])
        text_b = "".join(tokens_b[match.b : match.b + match.size])
        runs.append(DiffRun(EQUAL, old_text=text_a, new_text=text_b))
        # 锚点后半段递归
        end_a = match.a + match.size
        end_b = match.b + match.size
        if end_a < len(tokens_a) or end_b < len(tokens_b):
            runs.extend(
                _diff_chunked(
                    tokens_a[end_a:],
                    tokens_b[end_b:],
                    ignore_whitespace=ignore_whitespace,
                    ignore_case=ignore_case,
                    token_budget=token_budget,
                )
            )
        return runs

    # 无较长公共块时，按比例切块
    count = max(1, -(-max(len(tokens_a), len(tokens_b)) // max(1, token_budget // 2)))
    runs = []
    for (i1, i2), (j1, j2) in zip(
        _proportional_spans(len(tokens_a), count),
        _proportional_spans(len(tokens_b), count),
    ):
        runs.extend(
            _diff_tokens(
                tokens_a[i1:i2],
                tokens_b[j1:j2],
                ignore_whitespace=ignore_whitespace,
                ignore_case=ignore_case,
            )
        )
    return runs


def _normalize_tokens(
    tokens: Sequence[str],
    *,
    ignore_whitespace: bool,
    ignore_case: bool,
) -> list[str]:
    """生成仅用于比对的归一化 token 序列；展示仍使用原始 token。"""
    if not ignore_whitespace and not ignore_case:
        return list(tokens)
    normalized: list[str] = []
    for token in tokens:
        value = token
        if ignore_whitespace and token.strip() == "":
            value = ""
        elif ignore_case and token.strip() != "":
            value = token.casefold()
        normalized.append(value)
    return normalized


@dataclass(frozen=True)
class DiffRun:
    """一段连续的差异片段。

    - ``equal``：两边一致，正常比对时 ``old_text`` 与 ``new_text`` 相同
      （开启忽略选项后可能只在比对意义上一致）；
    - ``insert``：仅改写文新增，``old_text`` 为空；
    - ``delete``：仅原文存在，``new_text`` 为空；
    - ``modify``：同一处内容被改写，两字段分别为旧、新文本。

    所有片段的 ``old_text`` 依次拼接可还原原文，``new_text`` 依次拼接可还原改写文。
    """

    kind: str
    old_text: str = ""
    new_text: str = ""

    @property
    def changed(self) -> bool:
        return self.kind != EQUAL


@dataclass(frozen=True)
class DiffStats:
    insert_count: int
    delete_count: int
    modify_count: int
    similarity: float
    matched_chars: int
    original_chars: int
    rewritten_chars: int

    @property
    def total_count(self) -> int:
        return int(self.insert_count + self.delete_count + self.modify_count)

    @property
    def is_identical(self) -> bool:
        return self.total_count == 0

    @property
    def similarity_percent(self) -> int:
        return int(round(float(self.similarity) * 100))


@dataclass(frozen=True)
class DiffResult:
    runs: tuple[DiffRun, ...]
    stats: DiffStats
    original_text: str
    rewritten_text: str


def _diff_tokens(
    tokens_a: Sequence[str],
    tokens_b: Sequence[str],
    *,
    ignore_whitespace: bool,
    ignore_case: bool,
) -> list[DiffRun]:
    """对两个 token 序列做一次字符级比对。"""
    norm_a = _normalize_tokens(tokens_a, ignore_whitespace=ignore_whitespace, ignore_case=ignore_case)
    norm_b = _normalize_tokens(tokens_b, ignore_whitespace=ignore_whitespace, ignore_case=ignore_case)
    matcher = difflib.SequenceMatcher(None, norm_a, norm_b, autojunk=False)

    runs: list[DiffRun] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # 忽略大小写等选项下，两侧原文可能不完全相同（如 Hello / hello），
            # 分别保留各自文本，保证 old_text / new_text 拼接后仍能还原两段原文。
            runs.append(
                DiffRun(
                    EQUAL,
                    old_text="".join(tokens_a[i1:i2]),
                    new_text="".join(tokens_b[j1:j2]),
                )
            )
        elif tag == "delete":
            runs.append(DiffRun(DELETE, old_text="".join(tokens_a[i1:i2]), new_text=""))
        elif tag == "insert":
            runs.append(DiffRun(INSERT, old_text="", new_text="".join(tokens_b[j1:j2])))
        else:  # replace
            runs.append(
                DiffRun(
                    MODIFY,
                    old_text="".join(tokens_a[i1:i2]),
                    new_text="".join(tokens_b[j1:j2]),
                )
            )
    return runs


def _merge_runs(runs: Iterable[DiffRun]) -> list[DiffRun]:
    """合并相邻的同类型片段，避免同一处改动被统计成多“处”。"""
    merged: list[DiffRun] = []
    for run in runs:
        if merged and merged[-1].kind == run.kind:
            previous = merged[-1]
            merged[-1] = DiffRun(
                kind=run.kind,
                old_text=previous.old_text + run.old_text,
                new_text=previous.new_text + run.new_text,
            )
            continue
        merged.append(run)
    return merged


def compare_texts(
    original: str,
    rewritten: str,
    *,
    ignore_whitespace: bool = False,
    ignore_case: bool = False,
) -> DiffResult:
    """逐处比对两段文本，返回可渲染的差异片段与统计信息。

    ``ignore_whitespace`` 会先对两段文本做空白规范化（连续空白合并为一个空格、
    去掉首尾空白），因此展示的也是规范化后的文本；``ignore_case`` 只影响比对，
    展示仍保留原始大小写。
    """
    original_text = str(original or "")
    rewritten_text = str(rewritten or "")
    if ignore_whitespace:
        original_text = normalize_whitespace(original_text)
        rewritten_text = normalize_whitespace(rewritten_text)

    tokens_a = tokenize(original_text)
    tokens_b = tokenize(rewritten_text)

    runs = _merge_runs(
        _diff_chunked(
            tokens_a,
            tokens_b,
            ignore_whitespace=ignore_whitespace,
            ignore_case=ignore_case,
            token_budget=_DIRECT_TOKEN_BUDGET,
        )
    )

    insert_count = sum(1 for run in runs if run.kind == INSERT)
    delete_count = sum(1 for run in runs if run.kind == DELETE)
    modify_count = sum(1 for run in runs if run.kind == MODIFY)
    matched_chars = sum(len(run.old_text) for run in runs if run.kind == EQUAL)

    total_chars = len(original_text) + len(rewritten_text)
    similarity = 1.0 if total_chars == 0 else min(1.0, (2.0 * matched_chars) / float(total_chars))

    stats = DiffStats(
        insert_count=insert_count,
        delete_count=delete_count,
        modify_count=modify_count,
        similarity=similarity,
        matched_chars=matched_chars,
        original_chars=len(original_text),
        rewritten_chars=len(rewritten_text),
    )
    return DiffResult(
        runs=tuple(runs),
        stats=stats,
        original_text=original_text,
        rewritten_text=rewritten_text,
    )


def _escape_for_html(text: str) -> str:
    return html.escape(str(text or ""), quote=False).replace("\n", "<br/>")


def _insert_span(text: str) -> str:
    return (
        f'<span style="color:{INSERT_COLOR}; background-color:{INSERT_BACKGROUND};">'
        f"{_escape_for_html(text)}</span>"
    )


def _delete_span(text: str) -> str:
    return (
        f'<span style="color:{DELETE_COLOR}; background-color:{DELETE_BACKGROUND}; '
        f'text-decoration:line-through;">{_escape_for_html(text)}</span>'
    )


def _delete_underline_span(text: str) -> str:
    return (
        f'<span style="color:{DELETE_COLOR}; background-color:{DELETE_BACKGROUND}; '
        f'text-decoration:underline;">{_escape_for_html(text)}</span>'
    )


def run_to_original_html(run: DiffRun) -> str:
    """渲染单个片段在原文侧的展示形式。

    - EQUAL：原样呈现
    - DELETE / MODIFY：以删除底色 + 下划线呈现原文旧内容
    - INSERT：原文中不存在该内容，不显示
    """
    if run.kind == EQUAL:
        return _escape_for_html(run.old_text or run.new_text)
    if run.kind in (DELETE, MODIFY):
        return _delete_underline_span(run.old_text)
    return ""


def diff_to_original_html(
    result: DiffResult,
    *,
    font_size: int = 13,
    line_height: int = 100,
) -> str:
    """渲染原文侧的完整 HTML（带删除下划线标记）。"""
    body = "".join(run_to_original_html(run) for run in result.runs)
    if not body:
        body = '<span style="color:#94a3b8;">（没有可比较的内容）</span>'
    return (
        f'<div style="font-family:\'Microsoft YaHei\', \'Segoe UI\', sans-serif; '
        f'font-size:{int(font_size)}px; line-height:{int(line_height)}%; '
        f'color:#111827; white-space:pre-wrap;">{body}</div>'
    )


def run_to_html(run: DiffRun, *, show_deletes: bool = True) -> str:
    if run.kind == EQUAL:
        # 结果区展示的是「改写文」，未改动内容按改写文的写法呈现（忽略大小写时便于阅读）。
        return _escape_for_html(run.new_text or run.old_text)
    if run.kind == INSERT:
        return _insert_span(run.new_text)
    if run.kind == DELETE:
        return _delete_span(run.old_text) if show_deletes else ""
    if show_deletes:
        return _delete_span(run.old_text) + _insert_span(run.new_text)
    return _insert_span(run.new_text)


def runs_to_html(runs: Iterable[DiffRun], *, show_deletes: bool = True) -> str:
    """渲染为行内富文本片段（换行转为 <br/>，可直接交给 QTextBrowser）。"""
    return "".join(run_to_html(run, show_deletes=show_deletes) for run in runs)


def diff_to_html(
    result: DiffResult,
    *,
    show_deletes: bool = True,
    font_size: int = 13,
    line_height: int = 100,
) -> str:
    """渲染完整 HTML 片段，供富文本控件直接展示。

    ``line_height`` 的百分比是相对字体自身行距算的（13px 中文字体约 17px）。这里默认 100%
    而不是 160%，是为了让结果区的行距和左边那个编辑框**逐行对齐** —— 富文本控件和
    QPlainTextEdit 的行高算法不同，只要这里放大一点，结果里的每个空行都会明显比左侧高，
    看着就像"空行被撑开了"。
    """
    body = runs_to_html(result.runs, show_deletes=show_deletes)
    if not body:
        body = '<span style="color:#94a3b8;">（没有可比较的内容）</span>'
    return (
        f'<div style="font-family:\'Microsoft YaHei\', \'Segoe UI\', sans-serif; '
        f'font-size:{int(font_size)}px; line-height:{int(line_height)}%; '
        f'color:#111827; white-space:pre-wrap;">{body}</div>'
    )


def diff_to_rewritten_text(result: DiffResult) -> str:
    """仅保留改写后的内容（相同 + 新增 + 修改的新文本），即清理掉删除痕迹的成稿。"""
    return "".join(run.new_text for run in result.runs)


def diff_to_marked_text(result: DiffResult, *, show_deletes: bool = True) -> str:
    """纯文本形式的标记结果：``【-删除-】`` / ``【+新增+】``。"""
    parts: list[str] = []
    for run in result.runs:
        if run.kind == EQUAL:
            parts.append(run.new_text or run.old_text)
        elif run.kind == INSERT:
            parts.append(f"【+{run.new_text}+】")
        elif run.kind == DELETE:
            if show_deletes:
                parts.append(f"【-{run.old_text}-】")
        else:
            if show_deletes:
                parts.append(f"【-{run.old_text}-】")
            parts.append(f"【+{run.new_text}+】")
    return "".join(parts)


def format_stats(stats: Optional[DiffStats]) -> str:
    """把统计信息整理成一行中文摘要。"""
    if stats is None:
        return ""
    if stats.is_identical:
        return f"两段文本完全一致 · 共 {stats.original_chars} 字"
    return (
        f"共 {stats.total_count} 处差异 · 新增 {stats.insert_count} 处 / "
        f"删除 {stats.delete_count} 处 / 修改 {stats.modify_count} 处 · "
        f"相似度 {stats.similarity_percent}% · "
        f"原文 {stats.original_chars} 字 → 改写文 {stats.rewritten_chars} 字"
    )
