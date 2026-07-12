from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).parents[1]


def test_http_client_supports_socks_proxy_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")

    with httpx.Client():
        pass


def test_removed_runtime_surfaces_are_not_configured():
    conversation_utils = (REPO_ROOT / "src/khoj/processor/conversation/utils.py").read_text()
    color_utils = (REPO_ROOT / "src/interface/web/app/common/colorUtils.ts").read_text()
    icon_utils = (REPO_ROOT / "src/interface/web/app/common/iconUtils.tsx").read_text()
    logos = (REPO_ROOT / "src/interface/web/app/components/logo/khojLogo.tsx").read_text()

    assert not (REPO_ROOT / "docker-compose.yml").exists()
    for removed in ("construct_chat_history_for_operator", "to-image"):
        assert removed not in conversation_utils
    assert "getAvailableIcons" not in icon_utils
    assert "KhojAgentLogo" not in logos
    assert "convertColorToBorderClass" not in color_utils
    assert not (REPO_ROOT / "src/interface/web/app/components/loginPrompt/loginPrompt.module.css").exists()
