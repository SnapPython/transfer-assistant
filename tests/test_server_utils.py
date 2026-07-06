import pytest
from pathlib import Path

from transfer_assistant.server import AppConfig, LoginLockout, hotp, make_session_value, make_title_from_text, parse_bounded_int, sanitize_filename, validate_web_login, verify_session_value, verify_totp


def test_sanitize_filename_removes_path_parts_and_unsafe_chars():
    assert sanitize_filename("../a/bad:name?.txt") == "bad_name_.txt"


def test_sanitize_filename_uses_fallback_for_empty_names():
    assert sanitize_filename("...") == "upload.bin"


def test_make_title_from_text_uses_first_non_empty_line():
    assert make_title_from_text("\n\n  hello world\nsecond") == "hello world"


def test_parse_bounded_int_clamps_values():
    assert parse_bounded_int("500", 80, 1, 200) == 200
    assert parse_bounded_int("-5", 80, 1, 200) == 1
    assert parse_bounded_int("", 80, 1, 200) == 80


def test_parse_bounded_int_rejects_non_numbers():
    with pytest.raises(ValueError):
        parse_bounded_int("nan", 80, 1, 200)


def test_session_value_is_signed_and_token_scoped():
    value = make_session_value("secret-token")
    assert verify_session_value(value, "secret-token")
    assert not verify_session_value(value, "other-token")
    assert not verify_session_value(value + "tampered", "secret-token")


def test_totp_matches_rfc_6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert hotp(secret, 1, digits=8) == "94287082"
    assert verify_totp(secret, "94287082", at_time=59, digits=8, window=0)
    assert not verify_totp(secret, "00000000", at_time=59, digits=8, window=0)


def test_totp_only_web_login_does_not_accept_api_token():
    config = AppConfig(
        data_dir=Path("."),
        token="api-token",
        max_upload_bytes=1,
        max_text_chars=1,
        cors_origins=(),
        totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
        web_auth_mode="totp",
    )
    assert not validate_web_login({"token": "api-token"}, config)
    assert validate_web_login({"totp": hotp(config.totp_secret, int(__import__("time").time()) // 30)}, config)


def test_login_lockout_locks_after_three_consecutive_failures():
    now = 1_000.0
    limiter = LoginLockout(max_failures=3, lock_seconds=60, now=lambda: now)

    assert limiter.record_failure("203.0.113.10") == 0
    assert limiter.record_failure("203.0.113.10") == 0
    assert limiter.record_failure("203.0.113.10") == 60
    assert limiter.retry_after("203.0.113.10") == 60


def test_login_lockout_success_resets_failures():
    now = 1_000.0
    limiter = LoginLockout(max_failures=3, lock_seconds=60, now=lambda: now)

    assert limiter.record_failure("203.0.113.10") == 0
    assert limiter.record_failure("203.0.113.10") == 0
    limiter.record_success("203.0.113.10")

    assert limiter.record_failure("203.0.113.10") == 0
