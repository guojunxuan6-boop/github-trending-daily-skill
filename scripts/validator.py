#!/usr/bin/env python3
"""Validate a GitHub Trending Daily report against evidence and format rules."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

CARD_MARKER = "# 🚀 项目信息卡 "
REQUIRED = [
    "## 项目基本信息",
    "## 项目简介",
    "## 核心亮点",
    "## 技术 / 设计特点",
    "## 使用场景",
    "## 关注原因",
]


def validate_report(text: str, evidence: List[Dict[str, Any]], report_date: str) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    if f"日期：{report_date}" not in text:
        errors.append("report date does not match")
    generated = re.search(r"^生成时间：(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2}))$", text, re.MULTILINE)
    if not generated:
        errors.append("report generated time is missing or lacks a timezone")
    cards = re.split(r"(?=^# 🚀 项目信息卡 \d+)", text, flags=re.MULTILINE)[1:]
    if len(cards) != len(evidence):
        errors.append(f"card count {len(cards)} does not match evidence count {len(evidence)}")
    header_count = re.search(r"今日新增：(\d+) 个项目", text)
    if not header_count or int(header_count.group(1)) != len(evidence):
        errors.append("header item count does not match evidence")
    seen = set()
    by_repo = {item["repo"].casefold(): item for item in evidence}
    for index, card in enumerate(cards, 1):
        for heading in REQUIRED:
            if heading not in card:
                errors.append(f"card {index} missing {heading}")
        match = re.search(r"项目名称：\s*([^\n]+)", card)
        if not match:
            errors.append(f"card {index} missing repository name")
            continue
        repo = match.group(1).strip().casefold()
        if repo in seen:
            errors.append(f"duplicate repository {repo}")
        seen.add(repo)
        item = by_repo.get(repo)
        if not item:
            errors.append(f"card {index} repository not found in evidence: {repo}")
            continue
        url = item.get("github_url") or f"https://github.com/{item['repo']}"
        if url not in card or not url.startswith("https://github.com/"):
            errors.append(f"card {index} has invalid or mismatched GitHub URL")
        star_text = f"Star：{int(item.get('stars', 0)):,}"
        if star_text not in card:
            errors.append(f"card {index} Star value does not match evidence")
        growth = item.get("star_growth")
        growth_text = "首次记录，暂无基线" if growth is None else f"{int(growth):+,}"
        if f"今日增长：{growth_text}" not in card:
            errors.append(f"card {index} growth value does not match evidence")
        highlights = re.search(r"## 核心亮点\s*(.*?)(?:\n---|\n## )", card, re.DOTALL)
        highlight_count = len(re.findall(r"^- ", highlights.group(1), re.MULTILINE)) if highlights else 0
        if not 3 <= highlight_count <= 5:
            errors.append(f"card {index} must contain 3-5 highlights")
        uses = re.search(r"## 使用场景\s*(.*?)(?:\n---|\n## )", card, re.DOTALL)
        use_count = len(re.findall(r"^- ", uses.group(1), re.MULTILINE)) if uses else 0
        if not 2 <= use_count <= 3:
            errors.append(f"card {index} must contain 2-3 use cases")
        compact_length = len(re.sub(r"\s+", "", card))
        if compact_length < 300 or compact_length > 800:
            warnings.append(f"card {index} compact length {compact_length} is outside the preferred range")
        if item.get("data_quality") in ("partial", "fallback") and "数据质量" not in card:
            errors.append(f"card {index} must disclose partial/fallback data quality")
        if "推断" not in card:
            errors.append(f"card {index} attention reason must label inference")
    if re.search(r"\b(?:TODO|TBD|FIXME)\b|\[待补充\]", text, re.IGNORECASE):
        errors.append("report contains placeholders")
    return errors, warnings


def validate_file(path: Path, evidence: List[Dict[str, Any]], report_date: str) -> Tuple[List[str], List[str]]:
    return validate_report(Path(path).read_text(encoding="utf-8"), evidence, report_date)
