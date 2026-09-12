"""`make check-secrets` (spec §11): token shapes per service, configured values, a leftover .env."""
from __future__ import annotations

from backend import config
from backend.tools import check_secrets as cs

# Shapes are built from parts so this file does not itself trip the scan.
V4 = "-".join(["0123abcd", "4567", "4abc", "8def", "0123456789ab"])
NOT_V4 = "-".join(["11111111", "2222", "3333", "4444", "555555555555"])   # fake_claude.py's session id


def test_classic_patterns_still_fire():
    assert cs.suspicious("key = 'sk-ant-" + "a" * 24 + "'", "x.py") == "token-shaped string"
    assert cs.suspicious("Authorization: Bearer " + "b" * 30, "x.py") == "token-shaped string"
    assert cs.suspicious("Authorization: Token token=" + "c" * 20, "x.py") == "token-shaped string"
    assert cs.suspicious("plain line", "x.py") is None


def test_wanikani_uuid_needs_context_or_a_fixture_or_a_doc():
    assert cs.suspicious(f'"wanikani_token": "{V4}"', "settings.example.json")
    assert cs.suspicious(f"WANIKANI_TOKEN={V4}", "README.md")
    assert cs.suspicious(f'"token": "{V4}"', "backend/x.py")
    assert cs.suspicious(f'"id": "{V4}"', "backend/tests/fixtures/wanikani/user.json")
    assert cs.suspicious(f"the id {V4} is", "docs/notes.md")
    # VOICEVOX speaker ids are UUID v4 too: no context, not an SRS fixture, not a doc -> fine.
    assert cs.suspicious(f'"speaker_uuid": "{V4}",', "backend/tests/fixtures/voicevox/speakers.json") is None
    # A non-v4 UUID with token context is not the WaniKani shape.
    assert cs.suspicious(f"session token {NOT_V4}", "backend/tests/fake_claude.py") is None


def test_bunpro_assignment_shape():
    assert cs.suspicious('bunpro_api_token = "' + "x7" * 12 + '"', "x.py") == "Bunpro token assignment"
    assert cs.suspicious("BUNPRO_API_TOKEN: " + "y2" * 14, "x.yml") == "Bunpro token assignment"
    # Identifier-shaped values (no digit) and short placeholders are not a token.
    assert cs.suspicious('BUNPRO_TOKEN_OPT_IN_PARAM = "dangerously_authenticate_using_api_token"', "backend/constants.py") is None
    assert cs.suspicious('"BUNPRO_API_TOKEN": "bp-secret-value-1234"', "backend/tests/test_app.py") is None
    # The schema line and a short placeholder are not a token.
    assert cs.suspicious('Setting("BUNPRO_API_TOKEN", "", str, "account", "Bunpro -> Settings -> API -> Account API Token", secret=True),', "backend/config.py") is None
    assert cs.suspicious("ATAMA_BUNPRO_API_TOKEN=your-token", "README.md") is None


def test_configured_secret_value_is_found_whatever_its_shape():
    assert cs.suspicious("x = 'zz-odd-shape'", "x.py", secrets=["zz-odd-shape"]) == "a configured secret value"


def test_scan_reports_file_line_and_reason(tmp_path):
    good = tmp_path / "ok.py"
    good.write_text("print('hi')\n", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text(f"first\nWANIKANI_TOKEN={V4}\n", encoding="utf-8")
    binary = tmp_path / "face.glb"
    binary.write_bytes(b"glTF" + "Bearer ".encode() + b"x" * 40)
    hits = cs.scan(tmp_path, [good, bad, binary, tmp_path / "missing.py"], [])
    assert hits == ["bad.md:2: UUID next to wanikani/token (WaniKani token shape)"]


def test_dotenv_absent_harmless_and_leaking(tmp_path):
    assert cs.dotenv_report(tmp_path) == (None, [])
    env = tmp_path / ".env"
    env.write_text("SEARXNG_SECRET=abc\n", encoding="utf-8")
    message, leaked = cs.dotenv_report(tmp_path)
    assert message.startswith("warning") and leaked == []
    env.write_text("SEARXNG_SECRET=abc\nWANIKANI_TOKEN=wk-old-value-here\nCLAUDE_MODEL=opus\n", encoding="utf-8")
    message, leaked = cs.dotenv_report(tmp_path)
    assert message.startswith("FAIL") and leaked == ["wk-old-value-here"]
    assert "WANIKANI_TOKEN" in config.SECRET_KEYS       # what makes that line a leak
