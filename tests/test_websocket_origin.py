from khoj.routers.api_chat import is_allowed_websocket_origin


def test_websocket_origin_allows_forwarded_same_host():
    assert is_allowed_websocket_origin("http://10.106.17.252:12805", "10.106.17.252:12805")


def test_websocket_origin_rejects_different_host():
    assert not is_allowed_websocket_origin("http://evil.example", "10.106.17.252:12805")
