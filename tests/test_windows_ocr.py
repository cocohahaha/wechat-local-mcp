from __future__ import annotations

from wechat_local_mcp.windows_ocr import blocks_from_lines


def test_windows_ocr_coordinates_match_parser_convention() -> None:
    blocks = blocks_from_lines(
        [
            ("项目群", [(100, 100, 80, 20)]),
            ("请明天确认排期", [(100, 160, 60, 20), (165, 160, 100, 20)]),
        ],
        image_width=1000,
        image_height=500,
    )

    assert [block.text for block in blocks] == ["项目群", "请明天确认排期"]
    assert blocks[0].x == 0.1
    assert blocks[0].y == 0.76
    assert blocks[1].width == 0.165


def test_windows_ocr_skips_empty_lines() -> None:
    blocks = blocks_from_lines(
        [("", [(0, 0, 10, 10)]), ("有效", [])],
        image_width=100,
        image_height=100,
    )

    assert blocks == []
