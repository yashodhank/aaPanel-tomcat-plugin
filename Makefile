# JavaHost — Makefile
#
#   make test      — compile + run unit tests
#   make lint      — shellcheck + py_compile
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

.PHONY: test lint hooks zip deploy restart clean release

test:
	find $(SRC) -name '*.py' -print0 | xargs -0 $(PY) -m py_compile
	$(PY) -m pytest -q tests/

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
