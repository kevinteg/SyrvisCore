# Makefile for SyrvisCore
# Compatible with local development and GitHub Actions

.PHONY: all help clean test lint format format-check build-manager build-service build-spk build-dashboard test-dashboard test-mcp validate install dev-install dev-install-modern check check-modern version env env-modern

# Colors for output (disabled in CI)
ifdef CI
	GREEN=
	BLUE=
	YELLOW=
	RED=
	NC=
else
	GREEN=\033[0;32m
	BLUE=\033[0;34m
	YELLOW=\033[1;33m
	RED=\033[0;31m
	NC=\033[0m
endif

# Project paths
PROJECT_ROOT := $(shell pwd)
MANAGER_DIR := packages/syrviscore-manager
SERVICE_DIR := packages/syrviscore
MCP_DIR := packages/syrviscore-mcp
DASHBOARD_DIR := packages/syrviscore-dashboard
SRC_DIRS := $(MANAGER_DIR)/src $(SERVICE_DIR)/src
TESTS_DIR := tests
# Black + Ruff are static analyzers (they parse, never import), so they cover
# every package from the pinned 3.8 env even though mcp/dashboard target 3.10+.
# This is the union of what CI's per-job black/ruff steps check across the repo;
# keeping lint/format-check this wide is what stops `make check` going green while
# the mcp/dashboard CI jobs go red on formatting or lint.
LINT_DIRS := $(SRC_DIRS) $(MCP_DIR)/src $(DASHBOARD_DIR)/src \
             $(TESTS_DIR) $(MCP_DIR)/tests $(DASHBOARD_DIR)/tests
DIST_DIR := dist
BUILD_DIR := build
BUILD_TOOLS := build-tools
SPK_DIR := spk

# Version detection
VERSION := $(shell grep '^__version__' $(SERVICE_DIR)/src/syrviscore/__version__.py | cut -d'"' -f2)
MANAGER_VERSION := $(shell grep '^__version__' $(MANAGER_DIR)/src/syrviscore_manager/__version__.py | cut -d'"' -f2)
WHEEL_NAME := syrviscore-$(VERSION)-py3-none-any.whl
SPK_NAME := syrviscore-$(VERSION)-noarch.spk

# Python environment
PYTHON := python3
PIP := $(PYTHON) -m pip
PYTEST := $(PYTHON) -m pytest
BLACK := $(PYTHON) -m black
RUFF := $(PYTHON) -m ruff

# Pinned interpreter. `.python-version` (committed) selects the pyenv virtualenv,
# so the pyenv shims resolve $(PYTHON) to it from this directory even in a
# non-interactive shell (CI / agents / make) — no manual `pyenv activate`.
# `make env` bootstraps the virtualenv if it doesn't exist yet.
PY_ENV := $(shell cat .python-version 2>/dev/null || echo syrviscore)
# 3.8.12 matches Synology DSM's Python (see CLAUDE.md). Keep on its own line —
# a trailing `# comment` after `:=` would fold whitespace into the value.
PY_VERSION := 3.8.12

# mcp + dashboard target modern Python (>=3.10; fastmcp/fastapi ship no 3.8
# wheels) and run as their own CI jobs on 3.12 — never in the 3.8 SPK matrix. A
# parallel pyenv virtualenv hosts them so their pytest suites and the mcp
# seam-drift gen check are runnable locally without the 3.8 env fighting deps it
# can't resolve. `make env-modern` bootstraps it; PYENV_VERSION overrides
# .python-version for just these recipes (a version or virtualenv name wins over
# the committed pin without an activate step).
PY_VERSION_MODERN := 3.12.7
PY_ENV_MODERN := syrviscore-modern
MODERN_PYTHON := PYENV_VERSION=$(PY_ENV_MODERN) $(PYTHON)
MODERN_PYTEST := $(MODERN_PYTHON) -m pytest

# SSH deployment (for install target)
SSH_HOST ?=
SSH_USER ?= admin
SPK_REMOTE_PATH ?= /tmp/$(SPK_NAME)

##@ General

help: ## Display this help message
	@echo "$(BLUE)SyrvisCore Build System$(NC)"
	@echo "Version: $(GREEN)$(VERSION)$(NC)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"; printf "Usage:\n  make $(BLUE)<target>$(NC)\n"} \
		/^[a-zA-Z_-]+:.*?##/ { printf "  $(BLUE)%-15s$(NC) %s\n", $$1, $$2 } \
		/^##@/ { printf "\n$(YELLOW)%s$(NC)\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

version: ## Show current versions
	@echo "service: $(GREEN)$(VERSION)$(NC)  manager: $(GREEN)$(MANAGER_VERSION)$(NC)"

##@ Development

env: ## Bootstrap the pyenv virtualenv (creates it if missing) then dev-install
	@command -v pyenv >/dev/null 2>&1 || { printf "$(RED)[ERROR]$(NC) pyenv not found — run: brew install pyenv pyenv-virtualenv\n"; exit 1; }
	@pyenv virtualenv --version >/dev/null 2>&1 || { printf "$(RED)[ERROR]$(NC) pyenv-virtualenv not found — run: brew install pyenv-virtualenv\n"; exit 1; }
	@pyenv versions --bare | grep -qx "$(PY_VERSION)" || { printf "$(BLUE)[INFO]$(NC) Installing Python $(PY_VERSION) (matches DSM)...\n"; pyenv install -s "$(PY_VERSION)"; }
	@pyenv versions --bare | grep -qx "$(PY_ENV)" || { printf "$(BLUE)[INFO]$(NC) Creating virtualenv $(PY_ENV)...\n"; pyenv virtualenv "$(PY_VERSION)" "$(PY_ENV)"; }
	@$(MAKE) dev-install
	@printf "$(GREEN)[SUCCESS]$(NC) Env '$(PY_ENV)' ready. .python-version pins it — 'make test' / 'make check' work without activation.\n"

# mcp + dashboard are NOT installed here — they need Python 3.10+ (fastmcp/fastapi
# have no 3.8 wheels), so `pip install -e` refuses in this env. `make env-modern`
# builds their parallel 3.12 env; the 3.8 install stays manager + service only,
# matching the CI `test`/`dev-loop` jobs that also run on 3.8.
dev-install: ## Install manager + service in editable mode with dev dependencies (3.8)
	@echo "$(BLUE)[INFO]$(NC) Installing syrviscore-manager and syrviscore in development mode..."
	$(PIP) install -e "$(MANAGER_DIR)[dev]"
	$(PIP) install -e "$(SERVICE_DIR)[dev]"
	@echo "$(GREEN)[SUCCESS]$(NC) Development environment ready"
	@echo "Run 'syrvisctl --version' and 'syrvis --version' to verify installation"

env-modern: ## Bootstrap the 3.12 pyenv virtualenv for mcp + dashboard, then dev-install-modern
	@command -v pyenv >/dev/null 2>&1 || { printf "$(RED)[ERROR]$(NC) pyenv not found — run: brew install pyenv pyenv-virtualenv\n"; exit 1; }
	@pyenv virtualenv --version >/dev/null 2>&1 || { printf "$(RED)[ERROR]$(NC) pyenv-virtualenv not found — run: brew install pyenv-virtualenv\n"; exit 1; }
	@pyenv versions --bare | grep -qx "$(PY_VERSION_MODERN)" || { printf "$(BLUE)[INFO]$(NC) Installing Python $(PY_VERSION_MODERN) (matches the mcp/dashboard CI jobs)...\n"; pyenv install -s "$(PY_VERSION_MODERN)"; }
	@pyenv versions --bare | grep -qx "$(PY_ENV_MODERN)" || { printf "$(BLUE)[INFO]$(NC) Creating virtualenv $(PY_ENV_MODERN)...\n"; pyenv virtualenv "$(PY_VERSION_MODERN)" "$(PY_ENV_MODERN)"; }
	@$(MAKE) dev-install-modern
	@printf "$(GREEN)[SUCCESS]$(NC) Env '$(PY_ENV_MODERN)' ready — 'make test-mcp' / 'make test-dashboard' / 'make check-modern' run the modern-Python CI jobs locally.\n"

dev-install-modern: ## Editable-install mcp + dashboard (+ the service lib they import) into the 3.12 env
	@echo "$(BLUE)[INFO]$(NC) Installing syrviscore-mcp and syrviscore-dashboard (with the service lib) in development mode..."
	$(MODERN_PYTHON) -m pip install -e "$(SERVICE_DIR)" -e "$(MCP_DIR)[dev]" -e "$(DASHBOARD_DIR)[dev]"
	@echo "$(GREEN)[SUCCESS]$(NC) Modern-Python development environment ready"

check: lint format-check test ## Run all pinned-3.8 checks (lint + format-check + test). Add 'make check-modern' for the mcp/dashboard jobs.
	@echo "$(GREEN)[SUCCESS]$(NC) All checks passed!"

check-modern: test-mcp test-dashboard ## Run the modern-Python CI jobs (mcp + dashboard); needs 'make env-modern' first
	@echo "$(GREEN)[SUCCESS]$(NC) Modern-Python checks passed!"

##@ Code Quality

lint: ## Run ruff linter over every package (static; matches the CI ruff steps)
	@echo "$(BLUE)[INFO]$(NC) Running ruff linter..."
	$(RUFF) check $(LINT_DIRS)
	@echo "$(GREEN)[SUCCESS]$(NC) Linting passed"

format: ## Format code with black over every package
	@echo "$(BLUE)[INFO]$(NC) Formatting code with black..."
	$(BLACK) $(LINT_DIRS)
	@echo "$(GREEN)[SUCCESS]$(NC) Code formatted"

format-check: ## Check formatting over every package without changes (matches the CI black --check steps)
	@echo "$(BLUE)[INFO]$(NC) Checking code formatting..."
	$(BLACK) --check $(LINT_DIRS)

##@ Testing

test: ## Run tests with pytest
	@echo "$(BLUE)[INFO]$(NC) Running tests..."
	$(PYTEST) $(TESTS_DIR) -v
	@echo "$(GREEN)[SUCCESS]$(NC) Tests passed"

test-cov: ## Run tests with coverage report
	@echo "$(BLUE)[INFO]$(NC) Running tests with coverage..."
	$(PYTEST) $(TESTS_DIR) --cov=syrviscore --cov=syrviscore_manager --cov-report=term-missing --cov-report=html
	@echo "$(GREEN)[SUCCESS]$(NC) Coverage report generated in htmlcov/"

##@ Build

clean: ## Remove build artifacts and cache files
	@echo "$(BLUE)[INFO]$(NC) Cleaning build artifacts..."
	rm -rf $(DIST_DIR)/
	rm -rf $(BUILD_DIR)/
	rm -rf build-spk-tmp/
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf .ruff_cache/
	rm -rf $(MANAGER_DIR)/src/*.egg-info $(SERVICE_DIR)/src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "$(GREEN)[SUCCESS]$(NC) Build artifacts cleaned"

build-manager: ## Build manager wheel (+ bundled dependency wheels)
	@echo "$(BLUE)[INFO]$(NC) Building manager wheel..."
	chmod +x $(BUILD_TOOLS)/build-manager.sh
	./$(BUILD_TOOLS)/build-manager.sh

build-service: ## Build service wheel
	@echo "$(BLUE)[INFO]$(NC) Building service wheel..."
	chmod +x $(BUILD_TOOLS)/build-service.sh
	./$(BUILD_TOOLS)/build-service.sh

build-dashboard: ## Build the dashboard container image (docker; PUSH=1 to push, WITH_L2_TOOLS=true for L2)
	@echo "$(BLUE)[INFO]$(NC) Building dashboard image..."
	chmod +x $(BUILD_TOOLS)/build-dashboard.sh
	./$(BUILD_TOOLS)/build-dashboard.sh

test-dashboard: ## Run the dashboard package tests (Python 3.10+; mirrors the CI dashboard job — needs env-modern)
	@echo "$(BLUE)[INFO]$(NC) Running dashboard tests..."
	$(MODERN_PYTEST) $(DASHBOARD_DIR)/tests -v --tb=short

test-mcp: ## Run the mcp package tests + seam-drift gen check (Python 3.10+; mirrors the CI mcp job — needs env-modern)
	@echo "$(BLUE)[INFO]$(NC) Running mcp tests..."
	$(MODERN_PYTEST) $(MCP_DIR)/tests -v --tb=short
	@echo "$(BLUE)[INFO]$(NC) Checking generated deploy artifacts are in sync..."
	$(MODERN_PYTHON) -m syrviscore_mcp.deploy.gen check $(MCP_DIR)/deploy
	@echo "$(GREEN)[SUCCESS]$(NC) MCP checks passed"

build-spk: build-manager ## Build SPK package (manager only)
	@echo "$(BLUE)[INFO]$(NC) Building SPK package..."
	chmod +x $(BUILD_TOOLS)/build-spk.sh
	./$(BUILD_TOOLS)/build-spk.sh

tarball: ## Build the devkit tarball (dev loop: wheels + bootstrap.sh)
	chmod +x $(BUILD_TOOLS)/build-tarball.sh $(BUILD_TOOLS)/bootstrap.sh
	./$(BUILD_TOOLS)/build-tarball.sh
	@if [ -f "$(DIST_DIR)/$(SPK_NAME)" ]; then \
		echo "$(GREEN)[SUCCESS]$(NC) SPK built: $(SPK_NAME)"; \
		ls -lh $(DIST_DIR)/$(SPK_NAME); \
	else \
		echo "$(RED)[ERROR]$(NC) SPK build failed"; \
		exit 1; \
	fi

validate: ## Validate SPK package structure
	@if [ ! -f "$(DIST_DIR)/$(SPK_NAME)" ]; then \
		echo "$(RED)[ERROR]$(NC) SPK file not found. Run 'make build-spk' first."; \
		exit 1; \
	fi
	@echo "$(BLUE)[INFO]$(NC) Validating SPK package..."
	chmod +x $(BUILD_TOOLS)/validate-spk.sh
	./$(BUILD_TOOLS)/validate-spk.sh $(DIST_DIR)/$(SPK_NAME)

all: lint test build-spk ## Run all steps: lint + test + build-spk
	@echo "$(GREEN)======================================$(NC)"
	@echo "$(GREEN)[SUCCESS]$(NC) Complete build finished!"
	@echo "$(GREEN)======================================$(NC)"
	@echo "$(BLUE)[INFO]$(NC) Package ready: $(DIST_DIR)/$(SPK_NAME)"
	@echo ""
	@echo "Next steps:"
	@echo "  make validate         - Validate the SPK package"
	@echo "  make install          - Install to Synology (requires SSH_HOST)"

##@ Deployment

install: ## Install SPK to Synology via SSH (requires SSH_HOST variable)
	@if [ -z "$(SSH_HOST)" ]; then \
		echo "$(RED)[ERROR]$(NC) SSH_HOST variable not set"; \
		echo "Usage: make install SSH_HOST=192.168.0.100"; \
		exit 1; \
	fi
	@if [ ! -f "$(DIST_DIR)/$(SPK_NAME)" ]; then \
		echo "$(RED)[ERROR]$(NC) SPK file not found. Run 'make build-spk' first."; \
		exit 1; \
	fi
	@echo "$(BLUE)[INFO]$(NC) Copying SPK to $(SSH_HOST)..."
	scp $(DIST_DIR)/$(SPK_NAME) $(SSH_USER)@$(SSH_HOST):$(SPK_REMOTE_PATH)
	@echo "$(BLUE)[INFO]$(NC) Installing SPK on $(SSH_HOST)..."
	ssh $(SSH_USER)@$(SSH_HOST) "sudo synopkg install $(SPK_REMOTE_PATH)"
	@echo "$(GREEN)[SUCCESS]$(NC) SPK installed on $(SSH_HOST)"
	@echo ""
	@echo "Monitor installation logs:"
	@echo "  ssh $(SSH_USER)@$(SSH_HOST) 'tail -f /var/log/synopkg.log'"

nas-dev: tarball ## Ship the devkit to the NAS and bootstrap a dev install (requires SSH_HOST)
	@if [ -z "$(SSH_HOST)" ]; then \
		echo "$(RED)[ERROR]$(NC) SSH_HOST variable not set"; \
		echo "Usage: make nas-dev SSH_HOST=192.168.0.100"; \
		exit 1; \
	fi
	@echo "$(BLUE)[INFO]$(NC) Shipping devkit to $(SSH_HOST)..."
	scp dist/syrviscore-devkit-$(VERSION).tar.gz $(SSH_USER)@$(SSH_HOST):/tmp/
	ssh $(SSH_USER)@$(SSH_HOST) 'mkdir -p ~/syrviscore-devkit && \
		tar xzf /tmp/syrviscore-devkit-$(VERSION).tar.gz --strip-components=1 -C ~/syrviscore-devkit && \
		cd ~/syrviscore-devkit && ./bootstrap.sh --yes'
	@echo "$(GREEN)[SUCCESS]$(NC) Dev install bootstrapped on $(SSH_HOST)"

nas-dev-clean: ## Tear down the dev install on the NAS (requires SSH_HOST)
	@if [ -z "$(SSH_HOST)" ]; then \
		echo "$(RED)[ERROR]$(NC) SSH_HOST variable not set"; \
		exit 1; \
	fi
	ssh $(SSH_USER)@$(SSH_HOST) 'cd ~/syrviscore-devkit && ./bootstrap.sh --clean'

uninstall: ## Uninstall SPK from Synology via SSH (requires SSH_HOST variable)
	@if [ -z "$(SSH_HOST)" ]; then \
		echo "$(RED)[ERROR]$(NC) SSH_HOST variable not set"; \
		echo "Usage: make uninstall SSH_HOST=192.168.0.100"; \
		exit 1; \
	fi
	@echo "$(BLUE)[INFO]$(NC) Uninstalling syrviscore from $(SSH_HOST)..."
	ssh $(SSH_USER)@$(SSH_HOST) "sudo synopkg uninstall syrviscore"
	@echo "$(GREEN)[SUCCESS]$(NC) Package uninstalled"

##@ Docker Image Selection

select-docker-versions: ## Interactively select Docker image versions
	@echo "$(BLUE)[INFO]$(NC) Selecting Docker image versions..."
	chmod +x $(BUILD_TOOLS)/select-docker-versions.py
	$(PYTHON) $(BUILD_TOOLS)/select-docker-versions.py
	@echo "$(GREEN)[SUCCESS]$(NC) Docker versions updated in $(BUILD_DIR)/config.yaml"

##@ CI/CD

ci-install-deps: ## Install dependencies for CI
	@echo "$(BLUE)[INFO]$(NC) Installing CI dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install -e "$(MANAGER_DIR)[dev]"
	$(PIP) install -e "$(SERVICE_DIR)[dev]"

ci-build: ## CI build target (lint, test, build-spk)
	@echo "$(BLUE)[INFO]$(NC) Running CI build pipeline..."
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) build-spk
	@echo "$(GREEN)[SUCCESS]$(NC) CI build completed"

ci-test-only: ## CI test-only target (just run tests)
	@echo "$(BLUE)[INFO]$(NC) Running CI tests..."
	$(MAKE) test

##@ DSM Simulation

sim-setup: ## Initialize DSM 7.0 simulation environment
	@echo "$(BLUE)[INFO]$(NC) Setting up DSM 7.0 simulation..."
	chmod +x $(TESTS_DIR)/dsm-sim/setup-sim.sh
	chmod +x $(TESTS_DIR)/dsm-sim/bin/* 2>/dev/null || true
	./$(TESTS_DIR)/dsm-sim/setup-sim.sh
	@echo ""
	@echo "$(GREEN)[SUCCESS]$(NC) DSM simulation ready"
	@echo "Run: source $(TESTS_DIR)/dsm-sim/activate.sh"

sim-reset: ## Reset DSM simulation to clean state
	@echo "$(BLUE)[INFO]$(NC) Resetting DSM simulation..."
	chmod +x $(TESTS_DIR)/dsm-sim/reset-sim.sh
	./$(TESTS_DIR)/dsm-sim/reset-sim.sh

sim-clean: ## Remove DSM simulation entirely
	@echo "$(BLUE)[INFO]$(NC) Removing DSM simulation..."
	rm -rf $(TESTS_DIR)/dsm-sim/root
	rm -rf $(TESTS_DIR)/dsm-sim/state
	rm -rf $(TESTS_DIR)/dsm-sim/logs
	@echo "$(GREEN)[SUCCESS]$(NC) DSM simulation removed"

test-sim: sim-setup ## Run full simulation workflow test
	@echo "$(BLUE)[INFO]$(NC) Running simulation workflow test..."
	chmod +x $(TESTS_DIR)/test_sim_workflow.sh
	./$(TESTS_DIR)/test_sim_workflow.sh

test-versions: sim-setup ## Test version management (install, activate, rollback)
	@echo "$(BLUE)[INFO]$(NC) Running version management test..."
	chmod +x $(TESTS_DIR)/test_version_management.sh
	./$(TESTS_DIR)/test_version_management.sh
