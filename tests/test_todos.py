from wechat_local_mcp.models import ChatMessage
from wechat_local_mcp.todos import extract_todo_candidates, score_message


def test_request_with_deadline_scores_high() -> None:
    score, signals = score_message("麻烦你明天之前确认排期并回复我")

    assert score >= 3.0
    assert {"request", "action", "deadline"}.issubset(signals)


def test_plain_question_is_not_a_todo() -> None:
    score, signals = score_message("为什么不喜欢 codex")

    assert score == 0
    assert signals == []


def test_commitment_is_kept() -> None:
    messages = [
        ChatMessage(
            sender="ME",
            text="我明天整理好发给你",
            confidence=0.9,
            page=0,
        )
    ]

    candidates = extract_todo_candidates("项目群", messages, min_score=1.5)

    assert len(candidates) == 1
    assert candidates[0].sender == "ME"
