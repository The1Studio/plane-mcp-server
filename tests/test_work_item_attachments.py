"""Tests for attachment SSRF guard and metadata mapping (no network)."""

from unittest.mock import MagicMock, patch

import pytest

from plane_mcp.tools import work_item_attachments as wia


def _gai(*ips):
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


def _resp(status=200, location=None):
    r = MagicMock()
    r.status_code = status
    r.is_redirect = location is not None
    r.headers = {"Location": location} if location else {}
    return r


@pytest.mark.parametrize(
    "ips",
    [
        ["8.8.8.8", "127.0.0.1"],  # any private address rejects
        ["::ffff:127.0.0.1"],  # IPv4-mapped IPv6
        ["::ffff:10.0.0.1"],
        ["0.0.0.0"],
        ["0.1.2.3"],
        ["224.0.0.1"],
        ["ff02::1"],
        ["100.64.0.1"],
    ],
)
def test_assert_public_url_rejects(ips):
    with patch.object(wia.socket, "getaddrinfo", return_value=_gai(*ips)):
        with pytest.raises(ValueError, match="private/reserved"):
            wia._assert_public_url("https://example.com/x")


def test_assert_public_url_accepts_public():
    with patch.object(wia.socket, "getaddrinfo", return_value=_gai("8.8.8.8", "2606:4700::1111")):
        wia._assert_public_url("https://example.com/x")


def test_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        wia._assert_public_url("file:///etc/passwd")


def test_redirect_to_private_is_blocked():
    def gai(host, *_a, **_k):
        return _gai("127.0.0.1" if host == "internal.local" else "8.8.8.8")

    with (
        patch.object(wia.socket, "getaddrinfo", side_effect=gai),
        patch.object(wia._requests, "get", side_effect=[_resp(302, "http://internal.local/secret")]) as get,
    ):
        with pytest.raises(ValueError, match="private/reserved"):
            wia._fetch_public_url("https://example.com/x")
    assert get.call_count == 1
    assert get.call_args.kwargs["allow_redirects"] is False


def test_public_redirect_followed():
    final = _resp(200)
    with (
        patch.object(wia.socket, "getaddrinfo", return_value=_gai("8.8.8.8")),
        patch.object(wia._requests, "get", side_effect=[_resp(301, "/b"), final]) as get,
    ):
        assert wia._fetch_public_url("https://example.com/a") is final
    assert get.call_args_list[1].args[0] == "https://example.com/b"


def test_redirect_loop_limited():
    with (
        patch.object(wia.socket, "getaddrinfo", return_value=_gai("8.8.8.8")),
        patch.object(wia._requests, "get", side_effect=lambda *a, **k: _resp(302, "/loop")),
    ):
        with pytest.raises(ValueError, match="Too many redirects"):
            wia._fetch_public_url("https://example.com/a")


def test_attachment_to_dict_keeps_model_values_without_attributes():
    att = MagicMock()
    att.model_dump.return_value = {
        "id": "1",
        "name": "m.png",
        "content_type": "image/png",
        "size": 5,
        "attributes": None,
    }
    d = wia._attachment_to_dict(att, "ws")
    assert d["name"] == "m.png"
    assert d["content_type"] == "image/png"
    assert d["size"] == 5


def test_attachment_to_dict_prefers_attributes():
    att = MagicMock()
    att.model_dump.return_value = {
        "name": "old",
        "content_type": "a/b",
        "attributes": {"name": "n", "type": "t/x", "size": 9},
    }
    d = wia._attachment_to_dict(att, "ws")
    assert (d["name"], d["content_type"], d["size"]) == ("n", "t/x", 9)
