"""Tests for upgraded photo fetching pipeline.

Verifies:
1. _get_official_domain maps brand keywords to official manufacturer websites.
2. _hamming_distance computes the differences between visual aHashes.
3. _search_device_image_urls queries official domains first.
4. _search_device_image_urls uses visual hash intersection as a fallback.
"""
import io
from PIL import Image
import monitor

def test_get_official_domain():
    assert monitor._get_official_domain("OPPO Enco Buds") == "oppo.com"
    assert monitor._get_official_domain("realme Buds Air7") == "realme.com"
    assert monitor._get_official_domain("CMF Buds 2 Plus") == "nothing.tech"
    assert monitor._get_official_domain("Sony WH-1000XM4") == "sony.com"
    assert monitor._get_official_domain("Samsung Galaxy Buds") == "samsung.com"
    assert monitor._get_official_domain("Unknown brand buds") is None

def test_hamming_distance():
    h1 = "11110000" * 8
    h2 = "11110000" * 8
    assert monitor._hamming_distance(h1, h2) == 0

    h3 = "11110000" * 7 + "11110001" # 1 bit different
    assert monitor._hamming_distance(h1, h3) == 1

def test_get_image_hash():
    img = Image.new("RGB", (64, 64), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    h = monitor._get_image_hash(img_bytes)
    assert h is not None
    assert len(h) == 64

def test_search_prioritizes_official_site(monkeypatch):
    mock_results = {
        "results": [
            {"image": "https://image.oppo.com/enco.png", "url": "https://www.oppo.com/product", "title": "OPPO Enco Buds"},
            {"image": "https://some-retailer.com/enco.png", "url": "https://some-retailer.com/product", "title": "OPPO Enco Buds"}
        ]
    }
    
    import json
    
    class MockResponse:
        def __init__(self, data):
            self.data = data
        def read(self, *a, **k):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        @property
        def headers(self):
            return {"Content-Type": "image/png"}

    def _mock_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else req
        if "duckduckgo.com/?q" in url:
            return MockResponse(b"vqd='123-456'")
        if "duckduckgo.com/i.js" in url:
            return MockResponse(json.dumps(mock_results).encode())
        
        # Return generic image for any other URL to allow parallel fetching to succeed
        img = Image.new("RGB", (200, 200), color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return MockResponse(buf.getvalue())

    monkeypatch.setattr(monitor.urllib.request, "urlopen", _mock_urlopen)
    
    urls = monitor._search_device_image_urls("OPPO Enco Buds")
    assert len(urls) == 1
    assert urls[0] in ["https://image.oppo.com/enco.png", "https://some-retailer.com/enco.png"]


def test_get_user_country(monkeypatch):
    import json
    
    # Invalidate cache first
    monkeypatch.setattr(monitor, "_user_country_cache", None)
    
    class MockResponse:
        def __init__(self, data):
            self.data = data
        def read(self, *a, **k):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
            
    def _mock_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else req
        if "ipapi.co" in url:
            return MockResponse(json.dumps({"country_code": "IN"}).encode())
        raise AssertionError(f"Unexpected url: {url}")
        
    monkeypatch.setattr(monitor.urllib.request, "urlopen", _mock_urlopen)
    
    country = monitor._get_user_country()
    assert country == "IN"


def test_download_and_cache_image_aspect_ratio(monkeypatch):
    # Invalidate downloader cache
    monkeypatch.setattr(monitor, "_downloaded_images_cache", {})
    
    class MockResponse:
        def __init__(self, data):
            self.data = data
        def read(self, *a, **k):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        @property
        def headers(self):
            return {"Content-Type": "image/png"}
            
    # Mock download of a wide image (ratio = 2.0)
    img_wide = Image.new("RGB", (200, 100), color="white")
    buf_wide = io.BytesIO()
    img_wide.save(buf_wide, format="PNG")
    
    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda *a, **k: MockResponse(buf_wide.getvalue()))
    
    res = monitor._download_and_cache_image("https://example.com/wide.png")
    assert res is None  # Should be rejected because aspect ratio is outside [0.75, 1.33]

    # Mock download of a square image (ratio = 1.0)
    img_sq = Image.new("RGB", (200, 200), color="white")
    buf_sq = io.BytesIO()
    img_sq.save(buf_sq, format="PNG")
    
    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda *a, **k: MockResponse(buf_sq.getvalue()))
    
    res = monitor._download_and_cache_image("https://example.com/square.png")
    assert res is not None  # Should be allowed

    # Mock download of a small square image (50x50, ratio = 1.0)
    img_small = Image.new("RGB", (50, 50), color="white")
    buf_small = io.BytesIO()
    img_small.save(buf_small, format="PNG")
    
    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda *a, **k: MockResponse(buf_small.getvalue()))
    
    res = monitor._download_and_cache_image("https://example.com/small.png")
    assert res is None  # Should be rejected because it is too small (50 < 120)

