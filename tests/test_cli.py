import pytest

from khoj.utils.cli import cli, is_loopback_host


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.42.0.1", "::1"])
def test_loopback_hosts_allow_anonymous_mode(host):
    args = cli(["--host", host, "--anonymous-mode"])

    assert args.anonymous_mode is True
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.8", "offeragent.internal", "::"])
def test_lan_hosts_reject_anonymous_mode(host):
    with pytest.raises(SystemExit):
        cli(["--host", host, "--anonymous-mode"])


def test_lan_host_without_anonymous_mode_uses_token_boundary():
    args = cli(["--host", "0.0.0.0"])

    assert args.anonymous_mode is False


def test_unix_socket_rejects_anonymous_mode():
    with pytest.raises(SystemExit):
        cli(["--socket", "/tmp/offeragent.sock", "--anonymous-mode"])
