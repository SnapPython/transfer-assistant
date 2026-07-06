import pytest

from transfer_assistant.server import hotp, make_session_value, make_title_from_text, parse_bounded_int, sanitize_filename, verify_session_value, verify_totp


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
