from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.live_codex_vision_qualification import (
    CodexVisionQualification,
    LiveOfferAgentQualificationEnvironment,
)


@pytest.mark.live_codex_subscription
@pytest.mark.asyncio
async def test_every_live_codex_image_model_understands_the_same_ordered_interview_pages(tmp_path: Path) -> None:
    if os.environ.get("OFFERAGENT_RUN_LIVE_CODEX_VISION") != "1":
        pytest.skip("live Codex Subscription vision qualification is explicitly disabled")
    proxy_url = os.environ.get("OFFERAGENT_CODEX_PROXY_URL")
    if not proxy_url:
        pytest.fail("enabled live qualification requires OFFERAGENT_CODEX_PROXY_URL")
    font_path = Path(os.environ.get("OFFERAGENT_QUALIFICATION_CJK_FONT", r"C:\Windows\Fonts\NotoSansSC-VF.ttf"))
    environment = LiveOfferAgentQualificationEnvironment(
        proxy_url=proxy_url,
        font_path=font_path,
    )

    report = await CodexVisionQualification(environment).run(tmp_path / "live-codex-vision")

    print(report.to_json())
    assert report.status == "passed"
