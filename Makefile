PY ?= python3

.PHONY: help build icons test dev clean

help:
	@echo "make build   打包 fpk 到 dist/"
	@echo "make icons   重新生成图标"
	@echo "make test    跑单元测试"
	@echo "make dev     本地起服务（数据目录 ./.dev）"
	@echo "make clean   清理产物"

build:
	@$(PY) tools/build_fpk.py

icons:
	@$(PY) tools/make_icons.py

test:
	@$(PY) -m pytest -q tests || $(PY) tests/run_tests.py

dev:
	@mkdir -p .dev/etc .dev/var
	@TRIM_PKGETC=$(CURDIR)/.dev/etc TRIM_PKGVAR=$(CURDIR)/.dev/var \
		TRIM_APPDEST=$(CURDIR)/app $(PY) app/main.py

clean:
	@rm -rf dist build .dev/tmb.sqlite3* __pycache__ app/__pycache__ app/tmb/__pycache__
