.DEFAULT_GOAL := help

PYTHON ?= python3

.PHONY: help check test
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' "$(MAKEFILE_LIST)" | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

check: ## Check required executables, GTK bindings, and GUI environment
	@status=0; \
	$(PYTHON) opread.py --check-runtime || status=1; \
	if [ -n "$$WAYLAND_DISPLAY" ]; then echo 'GUI environment: WAYLAND_DISPLAY is set'; else echo 'GUI environment: WAYLAND_DISPLAY is not set'; fi; \
	if [ -n "$$DISPLAY" ]; then echo 'GUI environment: DISPLAY is set'; else echo 'GUI environment: DISPLAY is not set'; fi; \
	if [ -n "$$WAYLAND_DISPLAY" ]; then \
		if [ -n "$$XDG_RUNTIME_DIR" ]; then echo 'GUI environment: XDG_RUNTIME_DIR is set (Wayland)'; else echo 'GUI environment: XDG_RUNTIME_DIR is not set (Wayland)'; fi; \
	else echo 'GUI environment: XDG_RUNTIME_DIR not evaluated (Wayland not indicated)'; fi; \
	echo 'GUI environment checks report variable presence only; they do not prove a usable display'; \
	exit $$status

test: ## Run the offline unit tests
	@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_opread.py'
