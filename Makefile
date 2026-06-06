# JavaHost — Makefile
#
#   make test         — compile + run unit tests
#   make lint         — shellcheck + py_compile
#   make samples      — generate deploy-test fixtures into tests/fixtures/out/
#   make samples-db   — generate ALL DB artifacts (dbcheck.war + dbapp.jar per engine)
#   make test-deploy  — run the service-less deploy matrix E2E (needs a panel/Tomcat)
#   make matrix       — run the FULL Tomcat×Java×DB matrix E2E (needs a panel/Tomcat)
#   make matrix-dry   — print the full matrix plan + count (no host needed)
#   make hooks     — opt in to the local git pre-commit hook (.githooks)
#   make zip       — build distributable plugin zip (javahost.zip)
#   make deploy    — rsync plugin/javahost -> VPS plugin dir + restart panel (YOUR OWN panel)
#   make restart   — clear pycache + restart panel
#
# Override VPS_HOST on the command line as needed.

VPS_HOST    ?= root@217.217.248.180
PLUGIN_NAME  = javahost
PLUGIN_DST   = /www/server/panel/plugin/$(PLUGIN_NAME)
SRC          = plugin/$(PLUGIN_NAME)
PY          ?= python3

.PHONY: test lint hooks zip deploy restart clean release samples samples-db test-deploy matrix matrix-dry

test:
	find $(SRC) -name '*.py' -print0 | xargs -0 $(PY) -m py_compile
	$(PY) -m pytest -q tests/

samples:
	$(PY) tests/fixtures/make_samples.py --all

samples-db:
	$(PY) tests/fixtures/make_samples.py --db postgresql
	$(PY) tests/fixtures/make_samples.py --db mysql
	$(PY) tests/fixtures/make_samples.py --db mariadb
	$(PY) tests/fixtures/make_samples.py --db mongodb

test-deploy:
	$(PY) tests/e2e/deploy_matrix.py

matrix:
	$(PY) tests/e2e/matrix_full.py

matrix-dry:
	$(PY) tests/e2e/matrix_full.py --dry-run

lint:
	@command -v shellcheck >/dev/null 2>&1 && shellcheck -S warning $(SRC)/*.sh || echo "shellcheck not installed (skipped)"
	find $(SRC) -name '*.py' -print0 | xargs -0 $(PY) -m py_compile && echo "py_compile OK"

hooks:
	git config core.hooksPath .githooks
	@echo "Enabled .githooks (pre-commit). Disable with: git config --unset core.hooksPath"

zip:
	rm -f $(PLUGIN_NAME).zip
	cd plugin && zip -r ../$(PLUGIN_NAME).zip $(PLUGIN_NAME) -x '*/__pycache__/*' -x '*.pyc'
	@echo "Built $(PLUGIN_NAME).zip"

deploy:
	rsync -az --delete --exclude='__pycache__' --exclude='*.pyc' $(SRC)/ $(VPS_HOST):$(PLUGIN_DST)/
	$(MAKE) restart

restart:
	ssh $(VPS_HOST) "rm -rf $(PLUGIN_DST)/__pycache__ $(PLUGIN_DST)/core/*/__pycache__; /etc/init.d/bt restart"

release:
	@./scripts/release.sh $(filter-out $@,$(MAKECMDGOALS))

clean:
	rm -f $(PLUGIN_NAME).zip
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
