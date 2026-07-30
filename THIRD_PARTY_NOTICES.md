# Third-party notices

This project uses the MCP Python SDK, PyObjC, PyCryptodome, Zstandard,
Pillow, pywin32, PyWinRT, MLX Whisper, and the optional Frida Python bindings
as external dependencies. Please review their respective licenses in the
installed environment before redistribution.

The optional `media` extra also installs `pilk` to decode Tencent/WeChat SILK
voice data. `pilk` 0.2.4 declares GPL-3.0; it is imported only when local voice
transcription is requested. Distributors who bundle the optional media
dependencies must review and comply with its license.

The macOS window/OCR adapter is a clean implementation built around public
Apple APIs:

- Core Graphics window capture and input events
- Vision on-device text recognition

The Windows adapter is a clean implementation built around public Windows
APIs and Python projections:

- Win32 window discovery, capture, and input events
- Windows.Media.Ocr on-device text recognition

Design research also reviewed
[BiboyQG/WeChat-MCP](https://github.com/BiboyQG/WeChat-MCP), an MIT-licensed
Accessibility-based WeChat MCP server. No message-sending, contact-adding, or
Moments-publishing code is included here.
