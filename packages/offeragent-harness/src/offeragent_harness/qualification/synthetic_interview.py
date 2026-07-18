"""Deterministic fictional image fixture shared by built-product qualifications."""

# ruff: noqa: RUF001 -- Chinese punctuation is part of the qualification material.

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True, slots=True)
class SyntheticInterviewPage:
    index: int
    path: Path
    content_hash: str
    media_type: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SyntheticInterviewFixture:
    pages: tuple[SyntheticInterviewPage, ...]
    font_sha256: str


class SyntheticInterviewFixtureGenerator:
    """Render fixed fictional Chinese interview pages into one temporary root."""

    _PAGE_LINES = (
        (
            "OfferAgent 视觉资格素材 · 第 1 / 3 页",
            "以下公司与经历均为人工虚构",
            "公司：星河科技（虚构）",
            "岗位：后端工程师",
            "轮次：一面",
            "题目 Q1：如何处理 Redis 缓存击穿？",
            "跨页题 Q2：订单创建成功后，",
            "问题将在下一页继续。",
        ),
        (
            "OfferAgent 视觉资格素材 · 第 2 / 3 页",
            "跨页题 Q2（续）：支付回调重复到达时，",
            "如何保证幂等并避免重复扣款？",
            "轮次：二面",
            "题目 Q3：如何定位数据库慢查询？",
            "提示：本页承接上一页的 Q2。",
        ),
        (
            "OfferAgent 视觉资格素材 · 第 3 / 3 页",
            "轮次：终面",
            "题目 Q4：灰度发布失败后，",
            "如何设计快速回滚与验证方案？",
            "材料结束 · 页序为 1 → 2 → 3",
        ),
    )

    def __init__(self, *, font_path: Path) -> None:
        if not font_path.is_absolute() or not font_path.is_file():
            raise ValueError("qualification font must be an absolute regular file")
        self._font_path = font_path

    def generate(self, root: Path) -> SyntheticInterviewFixture:
        if not root.is_absolute():
            raise ValueError("qualification fixture root must be absolute")
        root.mkdir(parents=True, exist_ok=False)
        font_bytes = self._font_path.read_bytes()
        font_sha256 = f"sha256:{hashlib.sha256(font_bytes).hexdigest()}"
        font = ImageFont.truetype(str(self._font_path), 38)
        pages: list[SyntheticInterviewPage] = []
        for index, lines in enumerate(self._PAGE_LINES, start=1):
            path = root / f"page-{index}.png"
            image = Image.new("RGB", (1200, 1600), "white")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((55, 55, 1145, 1545), radius=18, outline="#263238", width=4)
            for line_index, line in enumerate(lines):
                draw.text((105, 110 + line_index * 145), line, fill="#111111", font=font)
            image.save(path, format="PNG", optimize=False, compress_level=9)
            payload = path.read_bytes()
            pages.append(
                SyntheticInterviewPage(
                    index=index,
                    path=path,
                    content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    media_type="image/png",
                    byte_length=len(payload),
                )
            )
        return SyntheticInterviewFixture(tuple(pages), font_sha256)


__all__ = [
    "SyntheticInterviewFixture",
    "SyntheticInterviewFixtureGenerator",
    "SyntheticInterviewPage",
]
