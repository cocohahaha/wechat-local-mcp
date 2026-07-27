from wechat_local_mcp.models import OcrBlock
from wechat_local_mcp.parser import names_match, parse_recent_chats


def block(text: str, x: float, y: float, width: float = 0.1) -> OcrBlock:
    return OcrBlock(
        text=text,
        confidence=0.95,
        x=x,
        y=y,
        width=width,
        height=0.02,
    )


def test_parse_recent_chats_uses_time_aligned_name() -> None:
    blocks = [
        block("> 项目群", 0.12, 0.80),
        block("请明天确认排期", 0.12, 0.76),
        block("13:27|", 0.29, 0.805, 0.025),
        block("搜索", 0.09, 0.92),
    ]

    chats = parse_recent_chats(blocks)

    assert len(chats) == 1
    assert chats[0].name == "项目群"
    assert chats[0].preview == "请明天确认排期"
    assert chats[0].time_label == "13:27"


def test_names_match_tolerates_spacing_and_decorations() -> None:
    assert names_match("✨ Vibe Friends 999 俱乐部 ✨", "VibeFriends999俱乐部")
