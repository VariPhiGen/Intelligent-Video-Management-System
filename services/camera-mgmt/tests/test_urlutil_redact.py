"""redact_credentials: camera passwords must never reach the logs.

Credentials are encrypted at rest (crypto.py) precisely so a database copy is
useless without the key — a plaintext copy in every `relay.add_path.ok` line
made that moot. The helper is a display form for logs, so what matters is that
the password is gone and everything a debugger needs (scheme, user, host, path)
survives.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.urlutil import inject_credentials, redact_credentials  # noqa: E402


def test_password_is_masked_and_everything_else_survives():
    out = redact_credentials("rtsps://admin:Hik%402026@192.168.31.35:554/video/live?channel=1")
    assert out == "rtsps://admin:***@192.168.31.35:554/video/live?channel=1"


def test_a_url_without_credentials_is_untouched():
    url = "rtsp://192.168.31.11:554/cam/realmonitor"
    assert redact_credentials(url) == url


def test_redacts_inside_arbitrary_text():
    """Error bodies (MediaMTX echoes the path config) and command lines pass
    through here too — the URL is embedded, not the whole string."""
    body = 'path "x" rejected: source rtsp://admin:pw@10.0.0.5:554/main unreachable'
    assert "pw" not in redact_credentials(body)
    assert "rtsp://admin:***@10.0.0.5:554/main" in redact_credentials(body)


def test_round_trip_with_inject_credentials():
    """Whatever inject_credentials can compose, redact_credentials must mask —
    including the URL-encoded '@' the injector's convention exists for."""
    url = inject_credentials("rtsp://10.0.0.5:554/main", "admin", "p@ss/wo:rd")
    out = redact_credentials(url)
    assert "p%40ss" not in out and "wo%3Ard" not in out and "***" in out
    assert out.startswith("rtsp://admin:***@10.0.0.5:554")


def test_multiple_urls_in_one_string():
    text = "main rtsp://a:s3cret@h1/x sub rtsps://b:hunter2@h2/y"
    out = redact_credentials(text)
    assert "s3cret" not in out and "hunter2" not in out
    assert out.count(":***@") == 2


# ── Hand-typed passwords: the cases the first regex missed ─────────────────
#
# The original matched lazily to the first '@' and banned '/', on the reasoning
# that inject_credentials URL-encodes the password. That holds for credentials
# this service composes — but POST /cameras stores an operator-typed rtsp_url
# verbatim (only the scheme is validated), so the raw forms below reach the
# logger. Measured against the old pattern: `my/pass` was not masked at all,
# and `p@ss` masked only its head, logging `***@ss@host`.

def test_a_raw_slash_in_the_password_is_still_masked():
    out = redact_credentials("rtsp://admin:my/pass@10.0.0.5:554/main")
    assert out == "rtsp://admin:***@10.0.0.5:554/main"
    assert "my/pass" not in out


def test_a_raw_at_sign_in_the_password_does_not_leak_its_tail():
    out = redact_credentials("rtsp://admin:p@ss@10.0.0.5:554/main")
    assert out == "rtsp://admin:***@10.0.0.5:554/main"
    assert "ss@10.0.0.5" not in out, "the tail after the first '@' used to survive"


def test_the_host_is_never_eaten_by_the_greedy_match():
    """Greedy-to-last-'@' is only safe because a host cannot contain '@'."""
    out = redact_credentials("rtsp://admin:p@ss@10.0.0.5:554/main and rtsp://u:x@host/b")
    assert "10.0.0.5:554/main" in out and "host/b" in out
    assert "p@ss" not in out and ":x@" not in out
