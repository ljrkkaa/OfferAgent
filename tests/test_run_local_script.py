import os
import subprocess
from pathlib import Path


RUN_LOCAL = Path(__file__).parents[1] / "scripts" / "run_local.sh"


def test_explicit_host_and_port_override_dotenv(tmp_path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("KHOJ_HOST=0.0.0.0\nKHOJ_PORT=12805\nKHOJ_API_KEY=kk-dotenv\n")
    executable = tmp_path / ".venv" / "bin" / "khoj"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$KHOJ_API_KEY" "$@"\n')
    executable.chmod(0o755)
    environment = os.environ | {
        "KHOJ_HOST": "127.0.0.1",
        "KHOJ_PORT": "42112",
        "KHOJ_API_KEY": "kk-explicit",
    }

    result = subprocess.run(
        [RUN_LOCAL],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "kk-explicit",
        "--host",
        "127.0.0.1",
        "--port",
        "42112",
        "--non-interactive",
        "--anonymous-mode",
    ]


def test_lan_host_requires_explicit_credentials_and_api_key(tmp_path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    executable = tmp_path / ".venv" / "bin" / "khoj"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KHOJ_ADMIN_EMAIL", "KHOJ_ADMIN_PASSWORD", "KHOJ_API_KEY", "KHOJ_PORT"}
    } | {"KHOJ_HOST": "0.0.0.0"}

    result = subprocess.run(
        [RUN_LOCAL],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "KHOJ_ADMIN_EMAIL" in result.stderr


def test_lan_host_starts_with_explicit_credentials_and_api_key(tmp_path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    executable = tmp_path / ".venv" / "bin" / "khoj"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n")
    executable.chmod(0o755)
    environment = {key: value for key, value in os.environ.items() if key != "KHOJ_PORT"} | {
        "KHOJ_HOST": "0.0.0.0",
        "KHOJ_ADMIN_EMAIL": "admin@example.com",
        "KHOJ_ADMIN_PASSWORD": "unique-password",
        "KHOJ_API_KEY": "kk-bootstrap-secret",
    }

    result = subprocess.run(
        [RUN_LOCAL],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "--host",
        "0.0.0.0",
        "--port",
        "42110",
        "--non-interactive",
    ]
