PY ?= python3

.PHONY: check compile test verify-units verify-shell live clean

check: compile test verify-units verify-shell

compile:
	$(PY) -m py_compile $$(find fart tests -name '*.py')

test:
	$(PY) -m pytest -q

# systemd-analyze is optional (not present on every dev machine).
verify-units:
	@if command -v systemd-analyze >/dev/null 2>&1; then \
		systemd-analyze verify deploy/*.service deploy/*.timer deploy/*.slice 2>&1 | grep -v -e 'Unit .* is not loaded properly' -e 'Cannot find unit' -e 'is not executable: No such file' || true; \
	else echo "systemd-analyze not available; skipping unit verification"; fi

verify-shell:
	bash -n deploy/install.sh deploy/backup.sh

# Opt-in 60 s live smoke test against PumpPortal (no key). Skips cleanly without network.
live:
	FART_LIVE=1 $(PY) -m pytest -q tests/test_live_smoke.py -s

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
