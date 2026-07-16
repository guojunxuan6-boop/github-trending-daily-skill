#!/usr/bin/env python3
"""Render a lightweight Chinese report draft from enriched repository evidence."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List


def clean_markdown(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`#>*_|~]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def readme_summary(readme: str) -> str:
    for block in re.split(r"\n\s*\n", readme):
        cleaned = clean_markdown(block)
        lowered = cleaned.casefold()
        if 30 <= len(cleaned) <= 400 and not lowered.startswith(("license", "installation", "contents", "badge")):
            return cleaned[:220]
    return "README 未提供可直接提炼的项目说明。"


def build_analysis_packet(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract conservative evidence; remote text is data, never instructions."""
    readme = item.get("readme", "")
    positioning = clean_markdown(item.get("description") or readme_summary(readme))[:240]
    bullets = []
    for line in readme.splitlines():
        stripped = line.strip()
        if re.match(r"^[-*+]\s+", stripped):
            value = clean_markdown(re.sub(r"^[-*+]\s+", "", stripped))
            attack_markers = ("ignore previous", "ignore all", "system prompt", "developer message", "reveal secret", "execute this command")
            if (
                12 <= len(value) <= 180
                and not value.casefold().startswith(("sponsor", "badge", "license"))
                and not any(marker in value.casefold() for marker in attack_markers)
            ):
                bullets.append(value)
        if len(bullets) >= 5:
            break
    topics = item.get("topics") or []
    use_cases = [f"评估其在「{topic}」相关项目中的适用性" for topic in topics[:3]]
    if len(use_cases) < 2:
        use_cases.extend(["验证项目核心能力与现有技术栈的兼容性", "跟踪版本、社区采用与实际限制"])
    lower = readme.casefold()
    security_notes = []
    for keyword, note in (
        ("jailbreak", "涉及提示词绕过时，仅用于授权安全评测。"),
        ("mitmproxy", "流量捕获可能接触会话凭据，应限定在本人账户和授权环境。"),
        ("proxy", "代理或隧道能力应结合当地法规与网络政策评估。"),
        ("token", "不得把访问令牌写入报告、缓存或仓库。"),
    ):
        if keyword in lower and note not in security_notes:
            security_notes.append(note)
    limitations = []
    for line in readme.splitlines():
        value = clean_markdown(line)
        if value and any(key in value.casefold() for key in ("limitation", "not for", "warning", "注意", "限制")):
            limitations.append(value[:200])
        if len(limitations) >= 3:
            break
    activity = [f"Stars {int(item.get('stars', 0)):,}", f"Forks {int(item.get('forks', 0)):,}"]
    if item.get("recent_commits"):
        activity.append(f"抓取到最近 {len(item['recent_commits'])} 条提交")
    if item.get("latest_release"):
        activity.append(f"最新 Release {(item['latest_release'] or {}).get('tag') or '已发布'}")
    return {
        "positioning": positioning,
        "features": bullets,
        "architecture": [],
        "use_cases": use_cases[:3],
        "limitations": limitations,
        "security_notes": security_notes,
        "activity_signals": activity,
        "source_urls": item.get("source_urls") or [item.get("github_url")],
        "data_quality": item.get("data_quality", "complete"),
        "attention_reason": (
            f"项目在候选快照中获得 {int(item.get('stars', 0)):,} Stars，"
            "结合近期创建、更新或增长信号，可能正在获得社区集中关注（推断）。"
        ),
    }


def highlights(item: Dict[str, Any]) -> List[str]:
    packet = item.get("analysis") or {}
    provided = packet.get("features") or []
    if len(provided) >= 3:
        return [clean_markdown(str(point))[:140] for point in provided[:5]]
    description = clean_markdown(item.get("description") or "暂未提供项目描述")[:100]
    points: List[str] = [
        f"项目定位：{description}",
        f"当前获得 {item.get('stars', 0):,} Stars、{item.get('forks', 0):,} Forks",
        f"主要开发语言：{item.get('language') or 'Unknown'}",
    ]
    topics = item.get("topics") or []
    if topics:
        points.append(f"覆盖主题：{'、'.join(topics[:5])}")
    commits = item.get("recent_commits") or []
    release = item.get("latest_release")
    if commits or release:
        activity = f"可见最近 {len(commits)} 条提交记录" if commits else "本次未抓取到近期提交"
        if release:
            activity += f"，最新版本为 {release.get('tag') or release.get('name') or 'latest'}"
        points.append(activity)
    if item.get("license"):
        points.append(f"采用 {item['license']} 开源许可证")
    return points[:5]


def card(item: Dict[str, Any], number: int) -> str:
    packet = item.get("analysis") or {}
    description = packet.get("positioning") or item.get("description") or readme_summary(item.get("readme", ""))
    topic_text = "、".join((item.get("topics") or [])[:3]) or "通用开发"
    commit_text = "近期存在可见提交" if item.get("recent_commits") else "当前抓取结果未发现近期提交证据"
    release_text = "并已有正式 Release" if item.get("latest_release") else "，暂未发现正式 Release"
    bullets = "\n".join(f"- {point}" for point in highlights(item))
    growth = item.get("star_growth")
    growth_text = "首次记录，暂无基线" if growth is None else f"{int(growth):+,}"
    quality = item.get("data_quality", "complete")
    quality_line = f"\n\n数据质量：{quality}" if quality in ("partial", "fallback") else ""
    architecture = packet.get("architecture")
    if isinstance(architecture, list):
        architecture = "。".join(clean_markdown(str(value)) for value in architecture[:5])
    technical = architecture or (
        f"项目主要使用 {item.get('language') or '未标注语言'}，围绕「{topic_text}」展开。"
        f"{commit_text}{release_text}。具体架构与实现边界需结合 README 和源码进一步确认。"
    )
    scenarios = packet.get("use_cases") or [
        "评估其是否适合现有技术栈或产品方案",
        f"快速验证与「{topic_text}」相关的原型",
        "跟踪项目迭代、社区采用与生态扩展",
    ]
    scenario_text = "\n".join(f"- {clean_markdown(str(value))}" for value in scenarios[:3])
    reason = packet.get("attention_reason") or (
        f"该项目在本次候选窗口中达到 {item.get('stars', 0):,} Stars；"
        "结合创建/更新时间与活动记录，可作为近期社区关注上升的候选信号（推断）。"
    )
    if "推断" not in reason:
        reason = f"{reason}（推断）"
    return f"""# 🚀 项目信息卡 {number}

## 项目基本信息

项目名称：{item['repo']}

GitHub：{item.get('github_url', f"https://github.com/{item['repo']}")}

Star：{item.get('stars', 0):,}

今日增长：{growth_text}

Language：{item.get('language') or 'Unknown'}{quality_line}

---

## 项目简介

一句话说明：{clean_markdown(description)[:240]}

---

## 核心亮点

{bullets}

---

## 技术 / 设计特点

{technical}

---

## 使用场景

{scenario_text}

---

## 关注原因

{reason}
"""


def render(items: List[Dict[str, Any]], report_date: str, generated_at: str = "") -> str:
    generated_at = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    header = (
        f"# GitHub Trending Daily\n\n日期：{report_date}\n\n"
        f"生成时间：{generated_at}\n\n今日新增：{len(items)} 个项目\n"
    )
    if not items:
        return f"{header}\n今日暂无新增项目。\n"
    return header + "\n---\n\n" + "\n\n---\n\n".join(card(item, i) for i, item in enumerate(items, 1)) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--date", default=date.today().isoformat())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("items", [])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(items, args.date), encoding="utf-8")
    print(f"wrote report with {len(items)} cards to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
