"""FIX-3: _ws_origin_allowed must accept only an absent Origin or a same-origin
http://127.0.0.1:PORT_HTTP / http://localhost:PORT_HTTP, and reject everything
else WITHOUT raising (mirrors the HTTP _is_trusted_origin semantics)."""
import monitor


def test_none_origin_allowed():
    assert monitor._ws_origin_allowed(None) is True


def test_empty_origin_allowed():
    assert monitor._ws_origin_allowed("") is True


def test_http_loopback_ip_correct_port_allowed():
    assert monitor._ws_origin_allowed(
        f"http://127.0.0.1:{monitor.PORT_HTTP}") is True


def test_http_localhost_correct_port_allowed():
    assert monitor._ws_origin_allowed(
        f"http://localhost:{monitor.PORT_HTTP}") is True


def test_wrong_port_rejected():
    assert monitor._ws_origin_allowed("http://127.0.0.1:9999") is False


def test_https_scheme_rejected():
    assert monitor._ws_origin_allowed(
        f"https://127.0.0.1:{monitor.PORT_HTTP}") is False


def test_external_domain_rejected():
    assert monitor._ws_origin_allowed("http://evil.com") is False


def test_suffix_spoofed_host_rejected():
    # classic bypass: the string starts with the trusted host but the real
    # host is evil.com -> parsed.port can't be cast -> must return False
    assert monitor._ws_origin_allowed(
        f"http://127.0.0.1:{monitor.PORT_HTTP}.evil.com") is False


def test_malformed_origin_rejected():
    # unbalanced bracket -> urlparse raises ValueError -> False (no exception)
    assert monitor._ws_origin_allowed("http://[") is False


def test_missing_port_rejected():
    # no explicit port -> parsed.port is None != PORT_HTTP
    assert monitor._ws_origin_allowed("http://127.0.0.1") is False
