# Tessera v1 — build & verify targets
#
# The release tarball payload is derived from the repo root:
#   src/  pyproject.toml  uv.lock  LICENSE  README.md  e2e/  install-modules/
# (see _PAYLOAD_INCLUDE in scripts/build_dist.py, filtered by .distignore).
#
# `make dist` stages the payload under dist/ (a GENERATED, git-ignored build
# output — never committed), builds the tarball + SHA256, and injects the real
# digest into dist/install.sh.
# `make check-dist` verifies the dist/ build is consistent and complete (CI gate,
# including the src/-module completeness gate).

UV ?= uv
PYTHON ?= python3
VERSION := $(shell $(PYTHON) -c "import re,pathlib;print(re.search(r'version\s*=\s*\"([^\"]+)\"',pathlib.Path('pyproject.toml').read_text()).group(1))")

.PHONY: help test dist check-dist clean

help:
	@echo "Targets:"
	@echo "  test         run the pytest suite (168 tests)"
	@echo "  dist         regenerate dist/ from sources + build tarball + inject SHA"
	@echo "  check-dist   verify dist/ is consistent and complete (CI gate)"
	@echo "  clean        remove the generated dist/ build output"

test:
	$(UV) run --extra dev pytest -q

dist:
	$(PYTHON) scripts/build_dist.py build

check-dist:
	$(PYTHON) scripts/build_dist.py check

clean:
	rm -rf dist/
