"""朱雀 AIGC 文本检测：接口调用 + 结果解析 + 报告渲染。

与 UI 无关（只用标准库），可直接单测。算法与 `zhuque_adapter.py`（本地 OpenAI 兼容适配器）
保持一致，但输出的是给 Qt 富文本用的 HTML，而不是 Markdown。

关键概念（上游返回的四个数口径不同，别混用）：
- ``ratio_confidence``：整体「疑似 AI 内容占比」＝ AI 占比 ＋ 疑似 AI 占比（逐段判定按字符数加权）；
- ``labels_ratio``：逐段判定按字符数加权的三类占比（人工 / AI / 疑似 AI）；
- ``softmax_confidence``：模型对整篇输出的整体 AI 置信度，与上面两个不是同一个数；
- ``segment_labels[].conf``：单段的 AI 置信度原始值（人工段越小越像人写的）。
"""

from __future__ import annotations

import html
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence


DEFAULT_UPSTREAM = "https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify"

LABELS = {"0": "人工撰写", "1": "AI 生成", "2": "疑似 AI 生成"}

# 语义配色：人工=绿、AI 生成=红、疑似 AI=琥珀，三套图表共用
PALETTE = (("人工撰写", "#1D9E75"), ("AI 生成", "#E24B4A"), ("疑似 AI 生成", "#EF9F27"))

# 输入框里的逐段底色（比图表色浅，压住正文但不影响阅读）
SEGMENT_TINTS = {0: "#D6F0E6", 1: "#FBDEDD", 2: "#FCEED3"}
# 没有逐段结果（合并段落）时，整篇按判定档位铺一层很淡的底色
VERDICT_TINTS = {"low": "#E4F4EA", "mid": "#FBF1DD", "high": "#FBE6E5"}

# 图表形式：qt 端能自己渲染 svg，画不出来时可用字符图兜底
CHART_SVG = "svg"
CHART_UNICODE = "unicode"
CHART_OFF = "off"
CHART_MODES: tuple[tuple[str, str], ...] = (
    (CHART_SVG, "SVG"),
    (CHART_UNICODE, "字符图"),
    (CHART_OFF, "关闭图表"),
)
DEFAULT_CHART_MODE = CHART_SVG

MERGE_MODES: tuple[tuple[bool, str], ...] = (
    (False, "逐段检测"),
    (True, "整篇检测"),
)
DEFAULT_IS_MERGE = False

# 逐段明细图表：最多画多少段（超出只统计不画）、片段预览留几个字
SEG_MAX = 40
SEG_SNIPPET = 22
# 甘特/横条刻度：把 0~1 映射到 0~100，一格 = 1%
SEG_SCALE = 100

# 图表 SVG 的设计宽度（窄窗口下按比例缩放）
CHART_WIDTH = 700
_CHART_WIDTH = CHART_WIDTH

# 图表版式（按 700 宽设计；卡片四周留 3px 余量，避免贴边时圆角被富文本裁掉）
_CARD_PAD = 3
_CARD_RX = 14
_ROW_Y0 = 88          # 第一条横条的中心线
_ROW_H = 32           # 行距
_BAR_H = 14           # 横条高度
_MIN_HEIGHT = 258     # 卡片最小高度（左栏环形图 + 图例要放得下）

# 报告用色（浅色主题）
_INK_MAIN = "#0f172a"
_INK_SUB = "#64748b"
_INK_BODY = "#334155"
_CARD_LINE = "#e5e7eb"
_CARD_FILL = "#f8fafc"
_TRACK = "#eef2f7"


class ZhuqueError(RuntimeError):
    """调用朱雀接口失败（网络、鉴权、上游返回异常都归到这里）。

    ``retryable`` 表示这次失败值不值得重试：网络类、超时、429、5xx 可以退避重试；
    401/403/400 这类业务错误重试也没用。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


# 自动重试的退避秒数（第 1 次重试前等 0.8s，第 2 次等 2s）
RETRY_DELAYS = (0.8, 2.0)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1


@dataclass(frozen=True)
class Segment:
    order: int
    label: int
    conf: float
    start: float
    length: float
    text: str

    @property
    def name(self) -> str:
        return LABELS.get(str(self.label), "未知")

    @property
    def color(self) -> str:
        if 0 <= self.label < len(PALETTE):
            return PALETTE[self.label][1]
        return _INK_SUB

    @property
    def strength(self) -> float:
        """条长用的「展示值」：人工段取 1−conf（人工度），AI / 疑似 AI 段取 conf。"""
        conf = max(0.0, min(1.0, float(self.conf)))
        return 1.0 - conf if self.label == 0 else conf


@dataclass
class ZhuqueReport:
    text: str
    raw: dict[str, Any]
    status: str = "success"
    ratio: float = 0.0
    human: float = 0.0
    ai_ratio: float = 0.0
    maybe: float = 0.0
    softmax: float = 0.0
    segments: tuple[Segment, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return str(self.status) in ("", "success", "None")

    @property
    def risk_ratio(self) -> float:
        return float(self.ai_ratio) + float(self.maybe)

    @property
    def verdict_level(self) -> str:
        """按整体 AI 置信度分档（阈值 70% / 40% 为本工具自定，官方未定义）。"""
        if self.softmax >= 0.7:
            return "high"
        if self.softmax >= 0.4:
            return "mid"
        return "low"

    @property
    def verdict_text(self) -> str:
        return {
            "high": "很可能是 AI 生成",
            "mid": "疑似 AI 生成",
            "low": "很可能是人工撰写",
        }[self.verdict_level]

    @property
    def divergence_note(self) -> str:
        """两项指标分歧时的提示。"""
        if self.human >= 0.6 and self.softmax >= 0.5:
            return (
                "**这两项指标分歧较大**：逐段判定以人工为主（%s），但整篇的整体 AI 置信度为 %s。"
                "前者是逐段判定的聚合，后者是模型对整篇的概率。"
                % (pct(self.human), pct(self.softmax))
            )
        if self.risk_ratio >= 0.5 and self.softmax < 0.4:
            return (
                "**这两项指标分歧较大**：逐段判定里 AI 与疑似内容合计 %s，但整篇的整体 AI "
                "置信度只有 %s。两项口径不同，建议结合逐段结果复核。"
                % (pct(self.risk_ratio), pct(self.softmax))
            )
        return ""

    @property
    def summary(self) -> str:
        """给历史列表用的一行摘要。"""
        return " · ".join(
            (
                pct(self.risk_ratio),
                "人工 %s" % pct(self.human),
                "%d 字" % len(self.text),
            )
        )


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: Any) -> str:
    try:
        return "%.1f%%" % (float(value) * 100)
    except (TypeError, ValueError):
        return "未知"


def mask_key(key: str) -> str:
    """打码显示 Key，避免完整密钥出现在界面、日志或截图里。"""
    key = str(key or "")
    if not key:
        return "(未设置)"
    if len(key) <= 12:
        return key[:3] + "*" * (len(key) - 3)
    return "%s…%s（共 %d 位）" % (key[:7], key[-4:], len(key))


def snippet(text: str, width: int = SEG_SNIPPET) -> str:
    text = (text or "").strip()
    return text if len(text) <= width else text[:width] + "…"


def verdict_color(score: float) -> str:
    if score >= 0.7:
        return "#A32D2D"
    if score >= 0.4:
        return "#854F0B"
    return "#3B6D11"


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _esc_attr(text: Any) -> str:
    return html.escape(str(text), quote=True)


class CancellationToken:
    """用于物理中断网络请求与耗时任务的取消令牌。"""

    def __init__(self) -> None:
        self.cancelled = False
        self._closer: Optional[Callable[[], None]] = None

    def register_closer(self, closer: Callable[[], None]) -> None:
        self._closer = closer
        if self.cancelled:
            try:
                closer()
            except Exception:
                pass

    def cancel(self) -> None:
        self.cancelled = True
        if self._closer is not None:
            try:
                self._closer()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 接口调用
# --------------------------------------------------------------------------- #
def call_zhuque(
    text: str,
    api_key: str,
    *,
    is_merge: bool = DEFAULT_IS_MERGE,
    upstream: str = DEFAULT_UPSTREAM,
    timeout: int = 60,
    cancel_token: Optional[CancellationToken] = None,
) -> dict[str, Any]:
    """调用朱雀检测接口，返回解析后的 dict；失败抛 :class:`ZhuqueError`。"""
    if cancel_token is not None and cancel_token.cancelled:
        raise ZhuqueError("检测已被取消。")

    key = str(api_key or "").strip()
    if not key:
        raise ZhuqueError("缺少 API Key：请在工具栏填写 EdgeOne Makers API Key（不会内置到程序里）。")
    if not str(text or "").strip():
        raise ZhuqueError("请先粘贴需要检测的文本。")

    payload = json.dumps({"text": text, "is_merge": bool(is_merge)}).encode("utf-8")
    request = urllib.request.Request(
        str(upstream or DEFAULT_UPSTREAM),
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=int(timeout))
        if cancel_token is not None:
            cancel_token.register_closer(response.close)
        try:
            body = response.read().decode("utf-8", "replace")
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        if cancel_token is not None and cancel_token.cancelled:
            raise ZhuqueError("检测已被取消。")
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code in (401, 403):
            raise ZhuqueError("鉴权失败（HTTP %s）：请检查 API Key 是否正确、是否已过期。%s" % (exc.code, detail))
        # 429 / 5xx 属于可恢复的：退避重试有意义
        retryable = exc.code == 429 or 500 <= exc.code < 600
        raise ZhuqueError("上游返回 HTTP %s：%s" % (exc.code, detail), retryable=retryable)
    except urllib.error.URLError as exc:
        if cancel_token is not None and cancel_token.cancelled:
            raise ZhuqueError("检测已被取消。")
        raise ZhuqueError("无法连接上游：%s（检查网络或代理）" % getattr(exc, "reason", exc), retryable=True)
    except TimeoutError:
        if cancel_token is not None and cancel_token.cancelled:
            raise ZhuqueError("检测已被取消。")
        raise ZhuqueError("请求超时：上游 %d 秒内没有返回，可稍后重试。" % int(timeout), retryable=True)
    except Exception as exc:
        if cancel_token is not None and cancel_token.cancelled:
            raise ZhuqueError("检测已被取消。")
        raise ZhuqueError("网络连接中断：%s" % exc, retryable=True)

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ZhuqueError("上游返回不是合法 JSON：%s" % exc)
    if not isinstance(result, dict):
        raise ZhuqueError("上游返回结构异常：期望 JSON 对象。")
    return result


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #
def segment_spans(raw_segments: Sequence[Any], total_chars: int) -> list[Segment]:
    """把 upstream 的 segment_labels 整理成 :class:`Segment` 列表。

    ``position`` 官方没写清是 [起点, 长度] 还是 [起点, 终点]：实测返回的是 [起点, 长度]
    （各段长度之和正好等于正文长度）。这里两种解释各算一遍总和，谁更贴近正文长度用谁，
    免得上游改口径后整张图的比例全歪。
    """
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_segments or []):
        if not isinstance(raw, dict):
            continue
        pos = raw.get("position")
        pos = list(pos) if isinstance(pos, (list, tuple)) else []
        start = _f(pos[0]) if len(pos) > 0 else 0.0
        second = _f(pos[1]) if len(pos) > 1 else 0.0
        try:
            label = int(raw.get("label"))
        except (TypeError, ValueError):
            label = -1
        try:
            order = int(raw.get("order"))
        except (TypeError, ValueError):
            order = index + 1
        rows.append(
            {
                "order": order,
                "label": label,
                "conf": _f(raw.get("conf")),
                "start": start,
                "second": second,
                "text": str(raw.get("text") or "").replace("\n", " ").strip(),
            }
        )
    rows.sort(key=lambda row: (row["order"], row["start"]))

    # 注意：这里必须用有符号求和。若上游给的是「长度」，second - start 会大量出负数，
    # 先 max(0, ·) 压成 0 反而会让错误的解释显得更贴近正文长度。
    sum_len = sum(row["second"] for row in rows)
    sum_end = sum(row["second"] - row["start"] for row in rows)
    use_span = total_chars > 0 and abs(sum_end - total_chars) < abs(sum_len - total_chars)

    return [
        Segment(
            order=row["order"],
            label=row["label"],
            conf=row["conf"],
            start=row["start"],
            length=max(0.0, row["second"] - row["start"]) if use_span else max(0.0, row["second"]),
            text=row["text"],
        )
        for row in rows
    ]


def parse_report(result: dict[str, Any], text: str) -> ZhuqueReport:
    """把接口返回整理成 :class:`ZhuqueReport`。"""
    labels = result.get("labels_ratio") or {}
    if not isinstance(labels, dict):
        labels = {}
    report = ZhuqueReport(
        text=str(text or ""),
        raw=dict(result or {}),
        status=str(result.get("status") or "success"),
        ratio=_f(result.get("ratio_confidence")),
        human=_f(labels.get("0", 0.0)),
        ai_ratio=_f(labels.get("1", 0.0)),
        maybe=_f(labels.get("2", 0.0)),
        softmax=_f(result.get("softmax_confidence")),
    )
    report.segments = tuple(segment_spans(result.get("segment_labels") or [], len(report.text)))
    return report


def segment_lens(segments: Sequence[Segment]) -> list[float]:
    """各段字符数；position 缺失（全 0）时退化成等宽，色带仍能画出来。"""
    lens = [max(0.0, float(seg.length)) for seg in segments]
    return lens if sum(lens) > 0 else [1.0] * len(segments)


def segment_tint(label: int) -> str:
    """逐段底色：认不出来的判定用中性灰白。"""
    return SEGMENT_TINTS.get(int(label), "#EEF2F7")


def verdict_tint(level: str) -> str:
    return VERDICT_TINTS.get(str(level), "#EEF2F7")


def segment_ranges(segments: Sequence[Segment], total_chars: int) -> list[tuple[int, int, Segment]]:
    """把逐段结果映射成输入文本里的字符区间（左闭右开），供输入框标底色。

    区间的右端优先取"下一段的起点"，比直接用 ``length`` 更稳（上游给的长度偶有偏差时不会
    出现缝或叠），最后一段铺到文末；全部夹在 ``[0, total_chars]`` 内且保证非空。
    """
    if total_chars <= 0 or not segments:
        return []
    ordered = sorted(segments, key=lambda seg: (seg.start, seg.order))
    ranges: list[tuple[int, int, Segment]] = []
    for index, seg in enumerate(ordered):
        start = int(round(seg.start))
        start = max(0, min(total_chars - 1, start))
        if index + 1 < len(ordered):
            end = int(round(ordered[index + 1].start))
        else:
            end = total_chars
        end = max(start + 1, min(total_chars, end))
        ranges.append((start, end, seg))
    return ranges


def full_range_tint(report: "ZhuqueReport") -> tuple[int, int, str]:
    """没有逐段结果时（合并段落），整篇按判定档位铺一层淡底色。"""
    return 0, len(report.text), verdict_tint(report.verdict_level)


def segment_counts(segments: Sequence[Segment]) -> dict[int, int]:
    counts = {0: 0, 1: 0, 2: 0}
    for seg in segments:
        if seg.label in counts:
            counts[seg.label] += 1
    return counts


def segment_summary(segments: Sequence[Segment]) -> str:
    counts = segment_counts(segments)
    return "共 %d 段 ｜ 人工撰写 %d 段 · AI 生成 %d 段 · 疑似 AI 生成 %d 段" % (
        len(segments),
        counts[0],
        counts[1],
        counts[2],
    )


# --------------------------------------------------------------------------- #
# 图表：SVG（左环右条合成卡片 / 整体占比卡）
# --------------------------------------------------------------------------- #
# 字宽估算（em 模型）：用来自己算对齐位置。
#
# 为什么不用 SVG 的 text-anchor：实测 Qt 的 QSvgRenderer 算出来的文本宽度比实际短很多
# （13px 的 "81.3%" 只按 ~37px 估算，实际 ~62px），于是 text-anchor="end" 的文本会从
# x 点向右溢出、被卡片边缘截掉尾巴（右侧数值只剩头一个字符）。自己按 em 估宽再左对齐
# 输出，位置最多差几个像素，但绝不会被裁掉。
_CHAR_WIDTH_EM = {".": 0.28, "%": 0.9, " ": 0.28, "=": 0.62, "-": 0.35, "/": 0.3, ":": 0.3, "+": 0.6}
_ASCII_WIDTH_EM = 0.56
_CJK_WIDTH_EM = 1.0


def text_width(text: Any, size: float) -> float:
    total = 0.0
    for char in str(text):
        if char in _CHAR_WIDTH_EM:
            total += _CHAR_WIDTH_EM[char]
        elif char.isascii():
            total += _ASCII_WIDTH_EM
        else:
            total += _CJK_WIDTH_EM
    return total * float(size)


def _svg_open(width: int, height: int) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
        'font-family="system-ui,-apple-system,&#39;Microsoft YaHei UI&#39;,&#39;PingFang SC&#39;,sans-serif">'
        % (width, height, width, height)
    )


def _svg_text(x: float, y: float, content: Any, size: float, fill: str, weight: str = "400", anchor: str = "start") -> str:
    """输出一段 SVG 文本；anchor 只影响算出来的 x，不交给渲染器处理。"""
    width = text_width(content, size)
    left = float(x)
    if anchor == "end":
        left = float(x) - width
    elif anchor == "middle":
        left = float(x) - width / 2.0
    return (
        '<text x="%.2f" y="%s" font-size="%s" fill="%s" font-weight="%s">%s</text>'
        % (left, y, size, fill, weight, _esc(content))
    )


def _donut_wedges(cx: float, cy: float, r_out: float, r_in: float, values: Sequence[float]) -> str:
    """把若干占比画成环形扇区，只用 <path d=… fill=…>，不碰 stroke-dasharray。

    某些客户端渲染链路会把 stroke-dasharray / stroke-dashoffset 丢掉，那样每条弧都会退化成
    整个圆环、后画的把先画的整圈盖掉。path + fill 没有这个问题。
    """

    def wedge(a0: float, a1: float, color: str) -> str:
        sweep = a1 - a0
        large = 1 if sweep > 180 else 0
        r0, r1 = math.radians(a0), math.radians(a1)
        d = " ".join(
            [
                "M %.2f %.2f" % (cx + r_out * math.cos(r0), cy + r_out * math.sin(r0)),
                "A %.1f %.1f 0 %d 1 %.2f %.2f"
                % (r_out, r_out, large, cx + r_out * math.cos(r1), cy + r_out * math.sin(r1)),
                "L %.2f %.2f" % (cx + r_in * math.cos(r1), cy + r_in * math.sin(r1)),
                "A %.1f %.1f 0 %d 0 %.2f %.2f"
                % (r_in, r_in, large, cx + r_in * math.cos(r0), cy + r_in * math.sin(r0)),
                "Z",
            ]
        )
        return '<path d="%s" fill="%s"/>' % (d, color)

    parts: list[str] = []
    start = -90.0  # 12 点起、顺时针
    for (_, color), value in zip(PALETTE, values):
        ratio = max(0.0, min(1.0, float(value)))
        if ratio <= 0.0005:
            continue
        if ratio >= 0.9995:
            # 满圈不能写成一段 A 命令（首尾重合会被判成零长度），拆成两个半圈
            parts.append(wedge(-90.0, 90.0, color))
            parts.append(wedge(90.0, 270.0, color))
            break
        # 末端多扫 0.35°：相邻扇区略微交叠，避免抗锯齿把交界处啃出一条白缝
        parts.append(wedge(start, start + ratio * 360.0 + 0.35, color))
        start += ratio * 360.0
    return "".join(parts)


def _svg_card(width: int, height: int) -> str:
    """卡片底：四周留 _CARD_PAD 余量，贴边显示时圆角不会被裁掉。"""
    pad = _CARD_PAD
    return (
        '<rect x="%d" y="%d" width="%d" height="%d" rx="%d" fill="%s" stroke="%s" stroke-width="1"/>'
        % (pad, pad, width - 2 * pad, height - 2 * pad, _CARD_RX, _CARD_FILL, _CARD_LINE)
    )


def build_svg_overview(human: float, ai_ratio: float, maybe: float, softmax: float) -> str:
    """整体占比卡：环形图（中心是整体 AI 置信度）+ 三条占比条。"""
    width, height = _CHART_WIDTH, 236
    radius, stroke, cx, cy = 44, 19, 116, 120
    values = (human, ai_ratio, maybe)
    parts = [
        _svg_open(width, height),
        _svg_card(width, height),
        _donut_wedges(cx, cy, radius + stroke / 2.0, radius - stroke / 2.0, values),
        _svg_text(cx, cy - 8, "AI 置信度", "11.5", _INK_SUB, anchor="middle"),
        _svg_text(cx, cy + 16, pct(softmax), "19", verdict_color(softmax), weight="500", anchor="middle"),
        _svg_text(228, 38, "内容构成占比", "14.5", _INK_MAIN, weight="500"),
    ]
    for index, ((name, color), value) in enumerate(zip(PALETTE, values)):
        y = 86 + index * 42
        ratio = max(0.0, min(1.0, float(value)))
        parts.append('<rect x="228" y="%d" width="12" height="12" rx="3" fill="%s"/>' % (y - 7, color))
        parts.append(_svg_text(250, y + 5, name, "13", "#444441"))
        parts.append('<rect x="380" y="%d" width="230" height="12" rx="6" fill="%s"/>' % (y - 7, _TRACK))
        if ratio > 0:
            parts.append(
                '<rect x="380" y="%d" width="%.1f" height="12" rx="6" fill="%s"/>' % (y - 7, ratio * 230, color)
            )
        parts.append(_svg_text(674, y + 5, pct(value), "13", _INK_MAIN, weight="500", anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


def build_svg_dashboard(segments: Sequence[Segment], human: float, ai_ratio: float, maybe: float) -> str:
    """合成卡片：左栏内容构成占比小环形图，右栏逐段判定强度横条表。"""
    width = _CHART_WIDTH
    left_x0, left_x1 = 18, 200
    divider_x = 214
    right_x0 = 232
    bar_x0, bar_x1 = 340, 628
    value_x = 674
    row_y0, row_h = _ROW_Y0, _ROW_H
    values = (human, ai_ratio, maybe)
    height = int(max(_MIN_HEIGHT, row_y0 + (len(segments) - 1) * row_h + 22))

    radius, stroke, cx, cy = 40, 17, 104, 98
    parts = [
        _svg_open(width, height),
        _svg_card(width, height),
        _svg_text(left_x0, 32, "内容构成占比", "14.5", _INK_MAIN, weight="500"),
        _donut_wedges(cx, cy, radius + stroke / 2.0, radius - stroke / 2.0, values),
        _svg_text(cx, cy - 7, "人工撰写", "10.5", _INK_SUB, anchor="middle"),
        _svg_text(cx, cy + 16, pct(human), "18", PALETTE[0][1], weight="500", anchor="middle"),
        '<line x1="%d" y1="48" x2="%d" y2="%d" stroke="%s" stroke-width="1"/>'
        % (divider_x, divider_x, height - 24, _CARD_LINE),
    ]
    for index, ((name, color), value) in enumerate(zip(PALETTE, values)):
        y = 170 + index * 28
        ratio = max(0.0, min(1.0, float(value)))
        parts.append('<rect x="%d" y="%d" width="11" height="11" rx="3" fill="%s"/>' % (left_x0, y - 9, color))
        parts.append(_svg_text(left_x0 + 17, y, name, "13", "#444441"))
        parts.append('<rect x="%d" y="%d" width="112" height="5" rx="3" fill="%s"/>' % (left_x0 + 17, y + 7, _TRACK))
        if ratio > 0:
            parts.append(
                '<rect x="%d" y="%d" width="%.1f" height="5" rx="3" fill="%s"/>'
                % (left_x0 + 17, y + 7, 112 * ratio, color)
            )
        parts.append(_svg_text(left_x1, y, pct(value), "13", _INK_MAIN, weight="500", anchor="end"))

    parts.append(_svg_text(right_x0, 32, "逐段判定强度", "14.5", _INK_MAIN, weight="500"))
    parts.append(_svg_text(value_x, 32, "条长 = 段名里的百分比", "11.5", _INK_SUB, anchor="end"))
    grid_end = height - 24
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = bar_x0 + (bar_x1 - bar_x0) * fraction
        parts.append(_svg_text("%.1f" % x, 58, "%d%%" % round(fraction * 100), "11", _INK_SUB, anchor="middle"))
        parts.append(
            '<line x1="%.1f" y1="66" x2="%.1f" y2="%d" stroke="%s" stroke-width="1"/>'
            % (x, x, grid_end, _CARD_LINE)
        )
    short = {0: "人工", 1: "AI", 2: "疑似AI"}
    for index, seg in enumerate(segments):
        y = row_y0 + index * row_h
        value = seg.strength
        parts.append(
            _svg_text(right_x0, y + 5, "段%s·%s" % (seg.order, short.get(seg.label, seg.name)), "13", "#444441")
        )
        parts.append(
            '<rect x="%d" y="%d" width="%d" height="%d" rx="7" fill="%s"/>'
            % (bar_x0, y - _BAR_H // 2, bar_x1 - bar_x0, _BAR_H, _TRACK)
        )
        if value > 0:
            parts.append(
                '<rect x="%d" y="%d" width="%.1f" height="%d" rx="7" fill="%s"/>'
                % (bar_x0, y - _BAR_H // 2, (bar_x1 - bar_x0) * value, _BAR_H, seg.color)
            )
        parts.append(_svg_text(value_x, y + 5, pct(value), "13", _INK_MAIN, weight="500", anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 图表：纯字符版
# --------------------------------------------------------------------------- #
def _disp_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad_right(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _bar(ratio: float, width: int, full: str = "█", empty: str = "░") -> str:
    filled = int(round(max(0.0, min(1.0, ratio)) * width))
    return full * filled + empty * (width - filled)


def _place(width: int, marks: Sequence[tuple[int, str]]) -> str:
    line: list[str] = []
    for col, text in sorted(marks):
        if len(line) < col:
            line.extend(" " * (col - len(line)))
        line.extend(text)
    return "".join(line)


def _ruler(width: int, ticks: Sequence[float]) -> str:
    chars = ["─"] * width
    chars[0] = "├"
    chars[width - 1] = "┤"
    for tick in ticks:
        chars[int(round(tick * (width - 1)))] = "┼"
    return "".join(chars)


def build_unicode_overview(human: float, ai_ratio: float, maybe: float, softmax: float) -> str:
    """纯字符仪表盘：不依赖任何渲染器。"""
    width = 40
    t40, t70 = int(round(0.4 * (width - 1))), int(round(0.7 * (width - 1)))
    heavy, thin = "═" * 48, "─" * 48
    items = (("人工撰写", human), ("AI 生成", ai_ratio), ("疑似 AI 生成", maybe))
    label_w = max(_disp_width(name) for name, _ in items) + 1
    rows = [
        "朱雀 AI 检测可视化",
        heavy,
        "  AI 生成置信度",
        "",
        "  " + _place(width, [(0, "0%"), (t40, "40%"), (t70, "70%"), (width - 4, "100%")]),
        "  " + _ruler(width, (0.4, 0.7)),
        "  " + _bar(softmax, width) + "  " + pct(softmax),
        "  " + _place(width, [(t40, "40%"), (t70, "70%")]),
        thin,
        "  内容构成",
        "",
    ]
    for name, value in items:
        rows.append("  " + _pad_right(name, label_w) + _bar(value, width) + "  " + pct(value))
    rows.append(heavy)
    return "\n".join(rows)


def build_unicode_segments(segments: Sequence[Segment]) -> str:
    """纯字符版逐段图：一条 48 格色带（人/Ａ/疑）+ 每段一行强度条。"""
    cols = 48
    marks = {0: "人", 1: "Ａ", 2: "疑"}
    lens = segment_lens(segments)
    total = sum(lens)
    cells: list[str] = []
    acc, end = 0.0, 0
    for seg, length in zip(segments, lens):
        acc += cols * length / total
        count = int(round(acc)) - end
        end += count
        cells.append(marks.get(seg.label, "?") * max(0, count))

    heavy, thin = "═" * cols, "─" * cols
    name_w = max([_disp_width(seg.name) for seg in segments] + [12]) + 1
    out = [
        "朱雀 AI 逐段判定",
        heavy,
        "  " + "".join(cells),
        "  人=人工撰写  Ａ=AI 生成  疑=疑似 AI 生成",
        thin,
        "",
    ]
    for seg in segments:
        out.append(
            "  段%-3s %s%s %s %s%s"
            % (
                seg.order,
                _pad_right(seg.name, name_w),
                _bar(seg.strength, 12),
                _pad_right(pct(seg.strength), 7),
                _pad_right("%d 字" % int(round(seg.length)), 8),
                snippet(seg.text, 24),
            )
        )
    out.append(heavy)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 原始返回（JSON 高亮）
# --------------------------------------------------------------------------- #
def _json_scalar(value: Any) -> str:
    if isinstance(value, str):
        return '<span style="color:#15803d;">%s</span>' % _esc(json.dumps(value, ensure_ascii=False))
    if isinstance(value, bool) or value is None:
        return '<span style="color:#7c3aed;">%s</span>' % _esc(json.dumps(value))
    return '<span style="color:#1d4ed8;">%s</span>' % _esc(value)


def json_to_html_lines(data: Any, indent: int = 0) -> list[str]:
    """把任意 JSON 结构渲染成带高亮的行列表（缩进两格）。"""
    pad = "  " * indent
    inner = "  " * (indent + 1)
    if isinstance(data, dict):
        if not data:
            return [pad + '<span style="color:#94a3b8;">{}</span>']
        lines = [pad + '<span style="color:#94a3b8;">{</span>']
        items = list(data.items())
        for index, (key, value) in enumerate(items):
            tail = "" if index == len(items) - 1 else '<span style="color:#94a3b8;">,</span>'
            body = json_to_html_lines(value, indent + 1)
            head = '%s<span style="color:#b91c1c;">"%s"</span><span style="color:#94a3b8;">: </span>' % (
                inner,
                _esc(key),
            )
            lines.append(head + body[0].strip())
            lines.extend(body[1:])
            lines[-1] = lines[-1] + tail
        lines.append(pad + '<span style="color:#94a3b8;">}</span>')
        return lines
    if isinstance(data, (list, tuple)):
        if not data:
            return [pad + '<span style="color:#94a3b8;">[]</span>']
        lines = [pad + '<span style="color:#94a3b8;">[</span>']
        for index, value in enumerate(data):
            tail = "" if index == len(data) - 1 else '<span style="color:#94a3b8;">,</span>'
            body = json_to_html_lines(value, indent + 1)
            lines.append(body[0])
            lines.extend(body[1:])
            lines[-1] = lines[-1] + tail
        lines.append(pad + '<span style="color:#94a3b8;">]</span>')
        return lines
    return [pad + _json_scalar(data)]


def json_to_html(data: Any) -> str:
    """原始返回区块：等宽字体 + JSON 语法高亮。"""
    body = "<br/>".join(json_to_html_lines(data))
    return (
        '<div style="margin:0 0 6px 0;">'
        '<span style="color:#475569; background:#f1f5f9; border-radius:4px; padding:1px 6px; '
        'font-size:11px; font-weight:700;">JSON</span></div>'
        '<div style="background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; padding:10px 12px; '
        'font-family:Consolas,&#39;Cascadia Mono&#39;,monospace; font-size:12.5px; white-space:pre; '
        'line-height:150%%;">%s</div>' % body
    )


# --------------------------------------------------------------------------- #
# 报告（HTML）
# --------------------------------------------------------------------------- #
def _note(text_html: str) -> str:
    return (
        '<div style="margin:8px 0 2px 0; background:#f8fafc; border-left:3px solid #cbd5e1; '
        'border-radius:0 8px 8px 0; padding:8px 12px; color:#64748b; font-size:12.5px; '
        'line-height:175%%;">%s</div>' % text_html
    )


def _bullet(text_html: str) -> str:
    return (
        '<div style="margin:3px 0; color:#334155; font-size:13px; line-height:185%%;">'
        '<span style="color:#94a3b8;">•</span>&nbsp;%s</div>' % text_html
    )


def _heading(text_html: str, *, top: int = 16) -> str:
    return (
        '<div style="margin:%dpx 0 6px 0; font-size:15px; font-weight:800; color:#0f172a;">%s</div>' % (top, text_html)
    )


def _chip(text: str, color: str) -> str:
    return (
        '<span style="color:%s; font-weight:700;">'
        '<span style="color:%s;">●</span> %s</span>' % (color, color, _esc(text))
    )


def _strong(text: str, color: str = "#0f172a") -> str:
    return '<span style="color:%s; font-weight:700;">%s</span>' % (color, _esc(text))


def _pre_block(text: str) -> str:
    return (
        '<div style="background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; padding:10px 12px; '
        'font-family:Consolas,&#39;Cascadia Mono&#39;,monospace; font-size:12.5px; white-space:pre; '
        'line-height:150%%; color:#334155;">%s</div>' % _esc(text)
    )


def segment_link_row(segments: Sequence[Segment], *, limit: int = SEG_MAX) -> str:
    """「定位：段1 段2 …」——点击可以跳到输入框里对应的那段（锚点由界面层接管）。

    注意 host 不能直接放纯数字：QUrl 会把 "seg://2" 的 host 当成 IPv4 解析成 "0.0.0.2"，
    所以用 "seg://segment/N"，界面层从 path 里取段号。
    """
    if not segments:
        return ""
    chips = " ".join(
        '<a href="seg://segment/%d" style="color:#475569; text-decoration:none;">'
        '<span style="background:#eef2f7; border-radius:4px; padding:1px 6px;">段%d</span></a>'
        % (seg.order, seg.order)
        for seg in segments[:limit]
    )
    return '<div style="margin:2px 0 6px 0; font-size:12.5px;">定位：%s</div>' % chips


def build_report_html(
    report: ZhuqueReport,
    *,
    chart_mode: str = DEFAULT_CHART_MODE,
    chart_image: str = "",
    chart_width: int = _CHART_WIDTH,
    with_links: bool = True,
) -> tuple[str, str]:
    """生成「朱雀 AI 文本检测结果」的 HTML。

    返回 ``(html, svg)``：``svg`` 非空时表示图表需要用 QSvgRenderer 渲染成图片，
    并以 ``chart_image`` 为名字注册进 QTextDocument 的图片资源里；``chart_image`` 为空时
    把 SVG 直接内嵌（导出 HTML 用，浏览器能渲染，Qt 富文本不行）。
    """
    if not report.ok:
        return (
            '<div style="font-size:14px; font-weight:800; color:#b91c1c;">检测失败</div>'
            + _bullet("status = %s" % _esc(report.status))
            + _bullet("请展开「原始返回」查看上游返回内容。"),
            "",
        )

    mode = str(chart_mode or DEFAULT_CHART_MODE).lower()
    if mode not in (CHART_SVG, CHART_UNICODE, CHART_OFF):
        mode = DEFAULT_CHART_MODE

    parts: list[str] = [
        _heading("朱雀 AI 文本检测结果", top=0),
        _bullet("文本长度：%s 字符" % _strong("%d" % len(report.text))),
        _bullet(
            "整体疑似 AI 内容占比（ratio_confidence）：%s ＝ AI %s ＋ 疑似 AI %s"
            % (_strong(pct(report.ratio)), _strong(pct(report.ai_ratio)), _strong(pct(report.maybe)))
        ),
        _bullet(
            "逐段判定构成（按字符数加权）：人工 %s ｜ AI %s ｜ 疑似 AI %s"
            % (_strong(pct(report.human)), _strong(pct(report.ai_ratio)), _strong(pct(report.maybe)))
        ),
        _note(
            "占比是按字符数加权统计的逐段判定（疑似 AI 内容占比 ＝ AI 占比 ＋ 疑似 AI 占比），"
            "<b>与「合并段落」开关无关</b>：合不合并段落，这几个数都一样，它只决定「逐段明细」的粒度。"
            "后者取自模型对整篇输出的 softmax 概率。人工占比高而整体置信度居中，是正常现象。"
        ),
    ]

    segments = report.segments
    svg = ""
    chart_html = ""

    if len(segments) >= 2:
        counts = segment_counts(segments)
        parts.append(_heading("逐段明细"))
        parts.append(_bullet(segment_summary(segments)))
        parts.append(
            _bullet(
                "图例：%s，%s，%s"
                % (
                    _chip("人工撰写", PALETTE[0][1]),
                    _chip("AI 生成", PALETTE[1][1]),
                    _chip("疑似 AI 生成", PALETTE[2][1]),
                )
            )
        )
        parts.append(
            _bullet(
                "横条长度 = 段名里的百分比，两者是同一个数：<b>人工段取 1 − AI 置信度</b>（人工度），"
                "<b>AI / 疑似 AI 段取 AI 置信度原值</b>——三种判定下都是<b>条越长 = 该判定越确定</b>"
            )
        )
        shown = segments[:SEG_MAX]
        if with_links:
            parts.append(segment_link_row(segments))
        if len(shown) < len(segments):
            parts.append(_bullet("图表只画前 %d 段，其余 %d 段未画" % (SEG_MAX, len(segments) - SEG_MAX)))
        if mode == CHART_OFF:
            for seg in segments:
                parts.append(
                    _bullet(
                        "段%s ［%s］ %s ｜ %s"
                        % (
                            seg.order,
                            _chip(seg.name, seg.color),
                            _strong(pct(seg.strength)),
                            _esc(snippet(seg.text, 60)),
                        )
                    )
                )
        elif mode == CHART_UNICODE:
            chart_html = _pre_block(build_unicode_segments(shown))
        else:
            svg = build_svg_dashboard(shown, report.human, report.ai_ratio, report.maybe)
    elif mode == CHART_SVG and any((report.human, report.ai_ratio, report.maybe)):
        svg = build_svg_overview(report.human, report.ai_ratio, report.maybe, report.softmax)
    elif mode == CHART_UNICODE and any((report.human, report.ai_ratio, report.maybe)):
        chart_html = _pre_block(build_unicode_overview(report.human, report.ai_ratio, report.maybe, report.softmax))

    if chart_html:
        parts.append('<div style="margin:10px 0 0 0;">%s</div>' % chart_html)
    elif svg and chart_image:
        parts.append(
            '<div style="margin:10px 0 0 0;"><img src="%s" width="%d"/></div>'
            % (_esc_attr(chart_image), int(chart_width))
        )
    elif svg:
        # 没有图片资源名时直接内嵌（导出 HTML 用）
        parts.append('<div style="margin:10px 0 0 0;">%s</div>' % svg)

    parts.append(_heading("整体判定"))
    parts.append(
        _bullet(
            "整体 AI 置信度（softmax_confidence）：%s"
            % _strong(pct(report.softmax), verdict_color(report.softmax))
        )
    )
    parts.append(
        _bullet(
            "综合判断（按整体 AI 置信度分档）：%s"
            % _strong(report.verdict_text, verdict_color(report.softmax))
        )
    )
    parts.append(_note("分档阈值 ≥70% / ≥40% 为本工具自定，官方未定义；与上面的占比口径不同，请以数值本身为准。"))

    divergence = report.divergence_note
    if divergence:
        parts.append(_note(divergence.replace("**", "<b>", 1).replace("**", "</b>", 1)))

    body = '<div style="font-size:13px;">' + "".join(parts) + "</div>"
    return body, svg


def report_summary_text(report: ZhuqueReport) -> str:
    """纯文本版摘要（复制到剪贴板用）。"""
    lines = [
        "朱雀 AI 文本检测结果",
        "文本长度：%d 字符" % len(report.text),
        "整体疑似 AI 内容占比：%s ＝ AI %s ＋ 疑似 AI %s"
        % (pct(report.ratio), pct(report.ai_ratio), pct(report.maybe)),
        "逐段判定构成：人工 %s ｜ AI %s ｜ 疑似 AI %s" % (pct(report.human), pct(report.ai_ratio), pct(report.maybe)),
        "整体 AI 置信度：%s" % pct(report.softmax),
        "综合判断：%s" % report.verdict_text,
    ]
    if report.segments:
        lines.append(segment_summary(report.segments))
        for seg in report.segments[:SEG_MAX]:
            lines.append(
                "段%s [%s] %s ｜ %s" % (seg.order, seg.name, pct(seg.strength), snippet(seg.text, 60))
            )
    return "\n".join(lines)


def report_to_plain_html(report: ZhuqueReport) -> str:
    """复制用的富文本（只保留标题 + 摘要，图表不带）。"""
    body, _ = build_report_html(report, chart_mode=CHART_OFF, with_links=False)
    return body


def report_to_markdown(report: ZhuqueReport, *, include_raw: bool = True) -> str:
    """导出用 Markdown：结论 + 逐段明细表 + 原始返回（折叠块）。"""
    if not report.ok:
        return "\n".join(
            [
                "## 检测失败",
                "",
                "- status = `%s`" % _esc(report.status),
                "",
                "```json",
                json.dumps(report.raw, ensure_ascii=False, indent=2),
                "```",
            ]
        )

    lines = [
        "## 朱雀 AI 文本检测结果",
        "",
        "- 文本长度：%d 字符" % len(report.text),
        "- 整体疑似 AI 内容占比（ratio_confidence）：%s ＝ AI %s ＋ 疑似 AI %s"
        % (pct(report.ratio), pct(report.ai_ratio), pct(report.maybe)),
        "- 逐段判定构成（按字符数加权）：人工 %s ｜ AI %s ｜ 疑似 AI %s"
        % (pct(report.human), pct(report.ai_ratio), pct(report.maybe)),
        "",
        "> 占比是按字符数加权统计的逐段判定（疑似 AI 内容占比 ＝ AI 占比 ＋ 疑似 AI 占比），"
        "**与「合并段落」开关无关**：它只决定「逐段明细」的粒度。"
        "它们与下方的「整体 AI 置信度」口径不同、**不是同一个数**。",
        "",
    ]
    if report.segments:
        lines += [
            "### 逐段明细",
            "",
            "- %s" % segment_summary(report.segments),
            "- 图例：人工撰写 / AI 生成 / 疑似 AI 生成",
            "",
            "| 段 | 判定 | 判定强度 | 字数 | 片段 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for seg in report.segments[:SEG_MAX]:
            lines.append(
                "| %s | %s | %s | %d | %s |"
                % (seg.order, seg.name, pct(seg.strength), int(round(seg.length)), snippet(seg.text, 40))
            )
        if len(report.segments) > SEG_MAX:
            lines.append("")
            lines.append("- 表格只列前 %d 段，其余 %d 段未列" % (SEG_MAX, len(report.segments) - SEG_MAX))
        lines.append("")
    lines += [
        "### 整体判定",
        "",
        "- 整体 AI 置信度（softmax_confidence）：**%s**" % pct(report.softmax),
        "- 综合判断（按整体 AI 置信度分档）：**%s**" % report.verdict_text,
        "",
        "> 分档阈值 ≥70% / ≥40% 为本工具自定，官方未定义；与上面的占比口径不同，请以数值本身为准。",
    ]
    if include_raw:
        lines += [
            "",
            "<details><summary>原始返回</summary>",
            "",
            "```json",
            json.dumps(report.raw, ensure_ascii=False, indent=2),
            "```",
            "",
            "</details>",
        ]
    return "\n".join(lines)


def report_to_html_document(report: ZhuqueReport, *, chart_mode: str = DEFAULT_CHART_MODE) -> str:
    """导出用完整 HTML 文档（图表是内嵌 SVG，浏览器直接能看）。"""
    body, _svg = build_report_html(report, chart_mode=chart_mode, chart_image="", with_links=False)
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\"/>\n"
        "<meta name=\"generator\" content=\"DeepCat 朱雀检测\"/>\n"
        "<title>朱雀 AI 文本检测结果</title>\n"
        "<style>body{font-family:\"Microsoft YaHei\",system-ui,sans-serif;color:#0f172a;"
        "background:#ffffff;margin:24px;line-height:1.7;}code{font-family:Consolas,monospace;}"
        "</style>\n</head>\n<body>\n%s\n</body>\n</html>\n" % body
    )


def chart_options() -> list[tuple[str, str]]:
    return list(CHART_MODES)


def chart_label(mode: str) -> str:
    for value, label in CHART_MODES:
        if value == str(mode):
            return label
    return CHART_MODES[0][1]
