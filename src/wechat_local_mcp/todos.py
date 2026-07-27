"""Deterministic todo-candidate scoring for Chinese and English chats."""

from __future__ import annotations

import re

from .models import ChatMessage, TodoCandidate


PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "request",
        re.compile(
            r"请|麻烦|辛苦|帮我|帮忙|记得|别忘|务必|需要你|请你|"
            r"能否|可不可以|可以.{0,8}[吗么]|please|could you|can you",
            re.I,
        ),
        1.25,
    ),
    (
        "action",
        re.compile(
            r"跟进|确认|回复|提交|发送|发给|整理|安排|完成|处理|检查|"
            r"更新|联系|预约|准备|提供|同步|交付|修改|审核|签字|付款|"
            r"报销|开票|排期|推进|上线|发布|下载|填写|继续做|编排|"
            r"follow up|confirm|reply|send|submit|finish|review|schedule|update",
            re.I,
        ),
        1.0,
    ),
    (
        "deadline",
        re.compile(
            r"今天|明天|后天|今晚|本周|下周|月底|周[一二三四五六日天]|"
            r"\d{1,2}[月/-]\d{1,2}[日号]?|"
            r"\d{1,2}:\d{2}|之前|截止|尽快|asap|by\s+\w+|deadline",
            re.I,
        ),
        1.0,
    ),
    (
        "commitment",
        re.compile(
            r"我来|我会|我负责|我安排|我处理|我跟进|我确认|我明天|我今天|"
            r"I(?:'ll| will)|let me",
            re.I,
        ),
        1.1,
    ),
    (
        "assignment",
        re.compile(r"你来|你负责|你先|你直接|交给你|@[\w\u4e00-\u9fff]+", re.I),
        0.75,
    ),
)

QUESTION_ONLY = re.compile(r"^(?:为什么|怎么|咋|what|why|how)\b", re.I)


def score_message(text: str) -> tuple[float, list[str]]:
    """Return a deterministic todo likelihood score and matching signals."""

    value = text.strip()
    if not value or QUESTION_ONLY.search(value):
        return 0.0, []

    score = 0.0
    signals: list[str] = []
    for label, pattern, weight in PATTERNS:
        if pattern.search(value):
            score += weight
            signals.append(label)

    if len(signals) >= 2:
        score += 0.25
    if value.endswith(("？", "?")) and "request" in signals:
        score += 0.25
    return round(score, 2), signals


def extract_todo_candidates(
    chat_name: str,
    messages: list[ChatMessage],
    *,
    min_score: float,
) -> list[TodoCandidate]:
    """Extract and sort likely todos from one chat."""

    candidates: list[TodoCandidate] = []
    seen: set[str] = set()
    for message in messages:
        score, signals = score_message(message.text)
        if score < min_score:
            continue
        dedupe_key = re.sub(r"\s+", "", message.text).casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidates.append(
            TodoCandidate(
                chat_name=chat_name,
                sender=message.sender,
                text=message.text,
                score=score,
                signals=signals,
            )
        )
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates
