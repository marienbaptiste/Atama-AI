# atama-AI — task entry points.
# No `make` on Windows? Every target is a plain python -m command; run those directly:
#   .venv/Scripts/python -m backend.tools.readonly_gate
#   .venv/Scripts/python -m pytest
#   .venv/Scripts/python -m backend.tools.doctor
PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo .venv/Scripts/python)

.PHONY: check-readonly check-secrets test doctor hooks capture-bunpro run avatar

# Golden Rule gate (spec §0). First target of `test`, prerequisite of `run` and `doctor`.
check-readonly:
	$(PY) -m backend.tools.readonly_gate

check-secrets:
	$(PY) -m backend.tools.check_secrets

test: check-readonly check-secrets
	$(PY) -m pytest

# The default avatar is fetched from TalkingHead's own repo rather than vendored here: it is
# CC BY-NC 4.0, so pulling it at setup keeps this tree cleanly MIT (LICENSE section 1).
avatar:
	$(PY) -m backend.tools.get_avatar

doctor: check-readonly
	$(PY) -m backend.tools.doctor

# Install the pre-commit hook that runs both checks.
hooks:
	$(PY) -m backend.tools.hooks

# One-off: capture real Bunpro responses (token from settings.json) into .cache/ for fixture pinning.
capture-bunpro: check-readonly
	$(PY) -m backend.tools.capture_bunpro

run: check-readonly
	@echo "make run lands with M2 (backend/app.py)"; exit 1
