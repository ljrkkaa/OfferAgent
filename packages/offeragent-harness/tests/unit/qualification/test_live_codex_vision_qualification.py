from __future__ import annotations

# ruff: noqa: RUF001 -- Chinese punctuation is expected semantic evidence.
from datetime import datetime, timezone
from pathlib import Path

import pytest
from scripts.live_codex_vision_qualification import (
    CodexVisionQualification,
    ImageModelEvidence,
    QualificationFailure,
    QualificationTeardownEvidence,
    SyntheticInterviewFixture,
    SyntheticInterviewFixtureGenerator,
    TextModelGateEvidence,
)

from offeragent_harness.providers.codex_subscription import (
    CodexCatalogModel,
    CodexModelCatalogSnapshot,
)


def _test_font() -> Path:
    candidates = (
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    )
    return next(path for path in candidates if path.is_file())


def test_synthetic_interview_pages_are_repeatable_ordered_and_private(tmp_path: Path) -> None:
    first = SyntheticInterviewFixtureGenerator(font_path=_test_font()).generate(tmp_path / "first")
    second = SyntheticInterviewFixtureGenerator(font_path=_test_font()).generate(tmp_path / "second")

    assert first.font_sha256 == second.font_sha256
    assert [page.index for page in first.pages] == [1, 2, 3]
    assert [page.content_hash for page in first.pages] == [page.content_hash for page in second.pages]
    assert [page.byte_length for page in first.pages] == [page.byte_length for page in second.pages]
    assert all(page.media_type == "image/png" for page in first.pages)
    assert all(page.path.parent == tmp_path / "first" for page in first.pages)
    assert all(page.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for page in first.pages)
    assert sorted(path.name for path in (tmp_path / "first").iterdir()) == [
        "page-1.png",
        "page-2.png",
        "page-3.png",
    ]


def _model(model_id: str, *, image: bool, original: bool) -> CodexCatalogModel:
    return CodexCatalogModel(
        model_id=model_id,
        display_name=model_id,
        description=None,
        input_modalities=("text", "image") if image else ("text",),
        supports_image_detail_original=original,
        supports_hosted_search=False,
        web_search_tool_type=None,
        context_window=128_000,
        max_context_window=128_000,
        effective_context_window_percent=95,
        additional_speed_tiers=(),
        service_tiers=(),
        default_service_tier=None,
    )


def _valid_semantics() -> dict[str, object]:
    return {
        "company": "星河科技（虚构）",
        "role": "后端工程师",
        "rounds": ["一面", "二面", "终面"],
        "pageOrder": [1, 2, 3],
        "crossPageQuestionId": "Q2",
        "crossPageQuestion": "订单创建成功后，支付回调重复到达时，如何保证幂等并避免重复扣款？",
        "finalPageTopic": "灰度发布失败后的快速回滚与验证",
    }


class _MatrixEnvironment:
    def __init__(self, *, semantics: dict[str, dict[str, object]] | None = None) -> None:
        self.models: tuple[CodexCatalogModel, ...] = (
            _model("vision-original", image=True, original=True),
            _model("vision-high", image=True, original=False),
            _model("text-only", image=False, original=False),
        )
        self.semantics = semantics or {
            "vision-original": _valid_semantics(),
            "vision-high": _valid_semantics(),
        }
        self.image_calls: list[tuple[str, str]] = []
        self.text_calls: list[str] = []
        self.generator = SyntheticInterviewFixtureGenerator(font_path=_test_font())

    def generate_fixture(self, root: Path) -> SyntheticInterviewFixture:
        return self.generator.generate(root)

    def fetch_catalog(self) -> CodexModelCatalogSnapshot:
        return CodexModelCatalogSnapshot(
            models=self.models,
            freshness="fresh",
            catalog_revision="sha256:" + "a" * 64,
            fetched_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
            account_binding="sha256:" + "b" * 64,
            error=None,
        )

    async def execute_image_model(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
        detail: str,
    ) -> ImageModelEvidence:
        del account_binding, fixture
        self.image_calls.append((model.model_id, detail))
        return ImageModelEvidence(self.semantics[model.model_id], 1, 3)

    async def exercise_text_model_gate(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
    ) -> TextModelGateEvidence:
        del account_binding, fixture
        self.text_calls.append(model.model_id)
        return TextModelGateEvidence("image_modality_unsupported", 0, 0)

    def verify_teardown(self) -> QualificationTeardownEvidence:
        return QualificationTeardownEvidence(auth_unchanged=True, temporary_state_removed=True)

    def report_metadata(self) -> dict[str, str]:
        return {"environment": "deterministic-fake"}


@pytest.mark.asyncio
async def test_qualification_runs_the_complete_dynamic_catalog_matrix(tmp_path: Path) -> None:
    environment = _MatrixEnvironment()

    report = await CodexVisionQualification(environment).run(tmp_path / "qualification")

    assert report.status == "passed"
    assert environment.image_calls == [("vision-original", "original"), ("vision-high", "high")]
    assert environment.text_calls == ["text-only"]
    assert [(item.model_id, item.status, item.detail, item.response_send_count) for item in report.models] == [
        ("vision-original", "qualified", "original", 1),
        ("vision-high", "qualified", "high", 1),
        ("text-only", "blocked_locally", None, 0),
    ]
    assert report.image_models_qualified == 2
    assert report.text_models_blocked == 1
    assert report.auth_unchanged is True
    assert report.temporary_state_removed is True


@pytest.mark.asyncio
async def test_one_image_model_missing_a_cross_page_fact_fails_the_whole_matrix(tmp_path: Path) -> None:
    incomplete = _valid_semantics()
    incomplete["crossPageQuestion"] = "订单创建成功后需要继续处理。"
    environment = _MatrixEnvironment(
        semantics={
            "vision-original": _valid_semantics(),
            "vision-high": incomplete,
        }
    )

    report = await CodexVisionQualification(environment).run(tmp_path / "qualification")

    assert report.status == "failed"
    failed = next(item for item in report.models if item.model_id == "vision-high")
    assert failed.status == "failed"
    assert failed.missing_facts == ("crossPageQuestion",)


@pytest.mark.asyncio
async def test_provider_failure_is_reported_for_its_model_and_teardown_still_runs(tmp_path: Path) -> None:
    class _FailingEnvironment(_MatrixEnvironment):
        def __init__(self) -> None:
            super().__init__()
            self.teardown_calls = 0

        async def execute_image_model(
            self,
            model: CodexCatalogModel,
            account_binding: str,
            fixture: SyntheticInterviewFixture,
            detail: str,
        ) -> ImageModelEvidence:
            if model.model_id == "vision-high":
                return ImageModelEvidence(None, 1, 3, error_code="provider_unreachable")
            return await super().execute_image_model(model, account_binding, fixture, detail)

        def verify_teardown(self) -> QualificationTeardownEvidence:
            self.teardown_calls += 1
            return super().verify_teardown()

    environment = _FailingEnvironment()

    report = await CodexVisionQualification(environment).run(tmp_path / "qualification")

    failed = next(item for item in report.models if item.model_id == "vision-high")
    assert report.status == "failed"
    assert failed.error_code == "provider_unreachable"
    assert failed.missing_facts == ()
    assert environment.teardown_calls == 1


@pytest.mark.asyncio
async def test_text_model_with_any_send_does_not_count_as_locally_blocked(tmp_path: Path) -> None:
    class _LeakyTextGateEnvironment(_MatrixEnvironment):
        async def exercise_text_model_gate(
            self,
            model: CodexCatalogModel,
            account_binding: str,
            fixture: SyntheticInterviewFixture,
        ) -> TextModelGateEvidence:
            del model, account_binding, fixture
            return TextModelGateEvidence("image_modality_unsupported", 0, 1)

    report = await CodexVisionQualification(_LeakyTextGateEnvironment()).run(tmp_path / "qualification")

    text_model = next(item for item in report.models if item.model_id == "text-only")
    assert report.status == "failed"
    assert text_model.status == "failed"
    assert text_model.response_send_count == 1


@pytest.mark.asyncio
async def test_catalog_without_an_image_model_cannot_pass(tmp_path: Path) -> None:
    environment = _MatrixEnvironment()
    environment.models = (_model("text-only", image=False, original=False),)

    report = await CodexVisionQualification(environment).run(tmp_path / "qualification")

    assert report.status == "failed"
    assert report.image_models_qualified == 0
    assert report.text_models_blocked == 1


@pytest.mark.asyncio
async def test_unexpected_execution_failure_still_invokes_teardown(tmp_path: Path) -> None:
    class _ExplodingEnvironment(_MatrixEnvironment):
        def __init__(self) -> None:
            super().__init__()
            self.teardown_calls = 0

        async def execute_image_model(
            self,
            model: CodexCatalogModel,
            account_binding: str,
            fixture: SyntheticInterviewFixture,
            detail: str,
        ) -> ImageModelEvidence:
            del model, account_binding, fixture, detail
            raise QualificationFailure("bounded diagnostic failure")

        def verify_teardown(self) -> QualificationTeardownEvidence:
            self.teardown_calls += 1
            return super().verify_teardown()

    environment = _ExplodingEnvironment()

    with pytest.raises(QualificationFailure, match="bounded diagnostic failure"):
        await CodexVisionQualification(environment).run(tmp_path / "qualification")

    assert environment.teardown_calls == 1
