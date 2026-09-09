from backend.status import StatusRegistry


def test_states_validated():
    r = StatusRegistry()
    r.report("wanikani", "ok", "level 12")
    try:
        r.report("wanikani", "connected")
    except ValueError:
        pass
    else:
        raise AssertionError


def test_sanitizer_masks_registered_secret_and_header_echo():
    r = StatusRegistry()
    # Deliberately short fakes so `make check-secrets` does not flag this file.
    r.register_secret("tok-abc-12")
    st = r.report("bunpro", "error", last_error="401 for Token token=tok-abc-12; Bearer zzzzzzzz")
    assert "tok-abc" not in st.last_error
    assert "zzzzzzzz" not in st.last_error


def test_listener_fires_on_change_only():
    r = StatusRegistry()
    seen = []
    r.subscribe(lambda s: seen.append(s.state))
    r.report("voicevox", "ok", "0.20")
    r.report("voicevox", "ok", "0.20")
    r.report("voicevox", "down")
    assert seen == ["ok", "down"]
