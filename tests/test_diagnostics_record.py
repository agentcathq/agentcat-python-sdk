"""Tests for OTLP record construction + severity inference."""

from mcpcat.modules import diagnostics


def test_warning_failed_is_error():
    """fail/error beats the Warning: prefix — a setup failure is ERROR."""
    rec = diagnostics._build_record_for_test("Warning: Failed to track server - boom")
    assert rec["severityText"] == "ERROR"
    assert rec["severityNumber"] == 17


def test_warning_is_warn():
    rec = diagnostics._build_record_for_test("Warning: something happened")
    assert rec["severityText"] == "WARN"
    assert rec["severityNumber"] == 13


def test_lowercase_warning_is_info():
    """'Warning:' is a case-sensitive literal; lowercase doesn't match."""
    rec = diagnostics._build_record_for_test("warning: nothing here")
    assert rec["severityText"] == "INFO"
    assert rec["severityNumber"] == 9


def test_plain_is_info():
    rec = diagnostics._build_record_for_test("AgentCat setup complete | project x")
    assert rec["severityText"] == "INFO"
    assert rec["severityNumber"] == 9


def test_bare_error_word_is_error():
    rec = diagnostics._build_record_for_test("Some error happened")
    assert rec["severityText"] == "ERROR"


def test_body_is_verbatim():
    entry = "[2026-01-01T00:00:00+00:00] arbitrary body text"
    rec = diagnostics._build_record_for_test(entry)
    assert rec["body"]["stringValue"] == entry


def test_record_shape():
    rec = diagnostics._build_record_for_test("anything")
    assert rec["timeUnixNano"].isdigit()
    assert rec["attributes"] == []
