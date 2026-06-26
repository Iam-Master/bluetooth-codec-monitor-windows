"""FIX-5 (audit S3): device-photo image URLs must be https to a *public* host.

_is_safe_image_url() blocks non-https schemes and SSRF targets (loopback,
link-local/metadata, private, reserved IPs, and obvious internal hostnames).
fetch_photo_for_device() re-checks the URL as defense-in-depth and must not
issue any network request for an unsafe URL.
"""
import monitor


def test_https_public_host_allowed():
    assert monitor._is_safe_image_url("https://cdn.rtings.com/a.jpg") is True
    assert monitor._is_safe_image_url("https://images-na.ssl-images-amazon.com/x.jpg") is True
    assert monitor._is_safe_image_url("https://8.8.8.8/x.png") is True  # public IP literal


def test_http_scheme_rejected():
    assert monitor._is_safe_image_url("http://rtings.com/a.jpg") is False


def test_non_web_schemes_rejected():
    for u in ("ftp://rtings.com/a.jpg", "file:///etc/passwd",
              "data:image/png;base64,AAAA", "javascript:alert(1)"):
        assert monitor._is_safe_image_url(u) is False, u


def test_loopback_rejected():
    assert monitor._is_safe_image_url("https://127.0.0.1/a.jpg") is False
    assert monitor._is_safe_image_url("https://[::1]/a.jpg") is False


def test_link_local_metadata_rejected():
    # AWS/cloud metadata endpoint — classic SSRF target.
    assert monitor._is_safe_image_url("https://169.254.169.254/latest/meta-data/") is False


def test_private_ipv4_ranges_rejected():
    for u in ("https://10.0.0.5/a.jpg", "https://172.16.0.1/a.jpg",
              "https://192.168.1.10/a.jpg"):
        assert monitor._is_safe_image_url(u) is False, u


def test_internal_hostnames_rejected():
    for u in ("https://localhost/a.jpg", "https://host.local/a.jpg",
              "https://svc.internal/a.jpg", "https://box.lan/a.jpg"):
        assert monitor._is_safe_image_url(u) is False, u


def test_public_hostnames_allowed():
    assert monitor._is_safe_image_url("https://example.com/a.jpg") is True
    assert monitor._is_safe_image_url("https://sub.cdn.example.org/p.webp") is True


def test_malformed_or_hostless_rejected():
    for u in ("", "https://", "not a url", "https:///path", "://nohost/a.jpg"):
        assert monitor._is_safe_image_url(u) is False, u


def test_fetch_refuses_unsafe_url_without_downloading(monkeypatch):
    # Proceed past the cache short-circuits, return an unsafe (http/loopback) URL
    # from search, and assert urlopen is NEVER called.
    monkeypatch.setattr(monitor, "get_photo_path", lambda name: None)
    monkeypatch.setattr(monitor, "_search_device_image_url",
                        lambda name: "http://127.0.0.1/evil.jpg")

    called = {"urlopen": False}

    def _boom(*a, **k):
        called["urlopen"] = True
        raise AssertionError("urlopen must not be called for an unsafe URL")

    monkeypatch.setattr(monitor.urllib.request, "urlopen", _boom)

    # Should return quietly without raising and without any network call.
    monitor.fetch_photo_for_device("Some Device")
    assert called["urlopen"] is False
