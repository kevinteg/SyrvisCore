# Makefile for SyrvisCore
# Compatible with local development and GitHub Actions

.PHONY: all help clean test lint format build-wheel build-spk validate install dev-install check version

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
SRC_DIR := src
TESTS_DIR := tests
DIST_DIR := dist
BUILD_DIR := build
BUILD_TOOLS := build-tools
SPK_DIR := spk

# Version detection
VERSION := $(shell grep '^__version__' src/syrviscore/__version__.py | cut -d'"' -f2)
WHEEL_NAME := syrviscore-$(VERSION)-py3-none-any.whl
SPK_NAME := syrviscore-$(VERSION)-noarch.spk

# Python environment
PYTHON := python3
PIP := $(PYTHON) -m pip
PYTEST := $(PYTHON) -m pytest
BLACK := $(PYTHON) -m black
RUFF := $(PYTHON) -m ruff

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

version: ## Show current version
	@echo "$(GREEN)$(VERSION)$(NC)"

##@ Development

dev-install: ## Install package in editable mode with dev dependencies
	@echo "$(BLUE)[INFO]$(NC) Installing syrviscore in development mode..."
	$(PIP) install -e ".[dev]"
	@echo "$(GREEN)[SUCCESS]$(NC) Development environment ready"
	@echo "Run 'syrvis --version' to verify installation"

check: lint test ## Run all checks (lint + test)
	@echo "$(GREEN)[SUCCESS]$(NC) All checks passed!"

##@ Code Quality

lint: ## Run ruff linter
	@echo "$(BLUE)[INFO]$(NC) Running ruff linter..."
	$(RUFF) check $(SRC_DIR) $(TESTS_DIR)
	@echo "$(GREEN)[SUCCESS]$(NC) Linting passed"

format: ## Format code with black
	@echo "$(BLUE)[INFO]$(NC) Formatting code with black..."
	$(BLACK) $(SRC_DIR) $(TESTS_DIR)
	@echo "$(GREEN)[SUCCESS]$(NC) Code formatted"

format-check: ## Check code formatting without making changes
	@echo "$(BLUE)[INFO]$(NC) Checking code formatting..."
	$(BLACK) --check $(SRC_DIR) $(TESTS_DIR)

##@ Testing

test: ## Run tests with pytest
	@echo "$(BLUE)[INFO]$(NC) Running tests..."
	$(PYTEST) $(TESTS_DIR) -v
	@echo "$(GREEN)[SUCCESS]$(NC) Tests passed"

test-cov: ## Run tests with coverage report
	@echo "$(BLUE)[INFO]$(NC) Running tests with coverage..."
	$(PYTEST) $(TESTS_DIR) --cov=$(SRC_DIR) --cov-report=term-missing --cov-report=html
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
	rm -rf src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "$(GREEN)[SUCCESS]$(NC) Build artifacts cleaned"

build-wheel: clean ## Build Python wheel package
	@echo "$(BLUE)[INFO]$(NC) Building Python wheel..."
	chmod +x $(BUILD_TOOLS)/build-python-package.sh
	./$(BUILD_TOOLS)/build-python-package.sh
	@if [ -f "$(DIST_DIR)/$(WHEEL_NAME)" ]; then \
		echo "$(GREEN)[SUCCESS]$(NC) Wheel built: $(WHEEL_NAME)"; \
	else \
		echo "$(RED)[ERROR]$(NC) Wheel build failed"; \
		exit 1; \
	fi

build-spk: build-wheel ## Build complete SPK package (includes wheel build)
	@echo "$(BLUE)[INFO]$(NC) Building SPK package..."
	chmod +x $(BUILD_TOOLS)/build-spk.sh
	./$(BUILD_TOOLS)/build-spk.sh
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

ci-install-deps: ## Install dependencies for CI (minimal, no dev tools)
	@echo "$(BLUE)[INFO]$(NC) Installing CI dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install build
	$(PIP) install -e .

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
