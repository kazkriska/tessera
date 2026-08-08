# Tessera v1 — build & verify targets
#
# Single source of truth for the release artifact lives in the repo root:
#   src/  pyproject.toml  uv.lock  LICENSE  e2e/  dist/install.sh  dist/install-modules/
# plus the hand-authored dist/README.md and dist/SOURCE_MANIFEST.txt.
#
# `make dist` regenerates the derived copies under dist/ and the
# tarball + SHA256, and injects the real digest into dist/install.sh.
# `make check-dist` verifies dist/ is NOT stale (used by CI).

UV ?= uv
PYTHON ?= python3
VERSION := $(shell $(PYTHON) -c "import re,pathlib;print(re.search(r'version\s*=\s*\"([^\"]+)\"',pathlib.Path('pyproject.toml').read_text()).group(1))")

.PHONY: help test dist check-dist clean

help:
	@echo "Targets:"
	@echo "  test         run the pytest suite (164 tests)"
	@echo "  dist         regenerate dist/ from sources + build tarball + inject SHA"
	@echo "  check-dist   verify dist/ is consistent with sources (CI gate)"
	@echo "  clean        remove build artifacts under dist/"

test:
	$(UV) run --extra dev pytest -q

dist:
	$(PYTHON) scripts/build_dist.py build

check-dist:
	$(PYTHON) scripts/build_dist.py check

clean:
	rm -rf dist/src dist/tessera-*.tar.gz dist/tessera-*.tar.gz.sha256
	rm -f dist/pyproject.toml dist/uv.lock dist/LICENSE dist/e2e/smoke_test.py
