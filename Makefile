.PHONY: install frontend-install frontend-build start lint clean build-manifold-engine unified-backtest strategy-yaml strategy-audit strategy-fingerprint parity-check push-config export-optimal-trades validate-live-runtime deploy-live

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PIP_FLAGS ?= --no-cache-dir
PIP_BREAK_FLAG ?= --break-system-packages
LINT_PATHS ?= scripts/trading scripts/research scripts/tools scripts/trading_service.py

CONFIG ?= config/optimization_smart_sweep.yaml
PARAMS_PATH ?= output/live_params.json
STRATEGY_PATH ?= config/mean_reversion_strategy.yaml
SIGNAL_TYPE ?= mean_reversion
TARGET ?= http://127.0.0.1:8000/api/strategy/update
USE_REGIME ?= 0
PROMOTED_PARAMS_PATH ?= config/live_params.json
VALIDATION_REDIS_URL ?= redis://localhost:6379/0
REQUIRE_ST_PEAK ?= 0

REGIME_FLAG := $(if $(filter 1 true yes,$(USE_REGIME)),--use-regime,)
CANONICAL_JSON_FLAG := $(if $(PROMOTED_PARAMS_PATH),--canonical-json-output $(PROMOTED_PARAMS_PATH),)
ST_PEAK_FLAG := $(if $(filter 1 true yes,$(REQUIRE_ST_PEAK)),--require-st-peak,)

install:
	$(PIP) install $(PIP_FLAGS) -r requirements.txt || \
		$(PIP) install $(PIP_FLAGS) $(PIP_BREAK_FLAG) -r requirements.txt

frontend-install:
	cd apps/frontend && npm install

frontend-build:
	cd apps/frontend && npm run build

start:
	$(PYTHON) scripts/trading_service.py

lint:
	$(PYTHON) -m compileall $(LINT_PATHS)

build-manifold-engine:
	@sh scripts/tools/build_manifold_engine.sh

clean:
	rm -rf __pycache__ */**/__pycache__ apps/frontend/node_modules apps/frontend/dist build src/build trader_core.egg-info src/trader_core.egg-info manifold_engine*.so src/manifold_engine*.so

unified-backtest:
	@$(PYTHON) scripts/tools/export_optimal_trades.py $(ARGS)

export-optimal-trades:
	@$(PYTHON) scripts/tools/export_optimal_trades.py $(ARGS)

strategy-yaml:
	@PYTHONPATH=. $(PYTHON) scripts/tools/json_to_yaml_strategy.py --params-path $(PARAMS_PATH) --output-path $(STRATEGY_PATH) --signal-type $(SIGNAL_TYPE) $(REGIME_FLAG) $(ST_PEAK_FLAG) $(CANONICAL_JSON_FLAG)

strategy-audit:
	@PYTHONPATH=. $(PYTHON) scripts/tools/audit_live_strategy.py --params-path $(PARAMS_PATH) --strategy-path $(STRATEGY_PATH) --signal-type $(SIGNAL_TYPE) $(REGIME_FLAG) $(ST_PEAK_FLAG)

strategy-fingerprint:
	@git rev-parse HEAD
	@if ! git diff --quiet --ignore-submodules --exit-code || ! git diff --cached --quiet --ignore-submodules --exit-code; then echo "WORKTREE_DIRTY"; fi
	@sha256sum $(STRATEGY_PATH)
	@if [ -f "$(PARAMS_PATH)" ]; then sha256sum "$(PARAMS_PATH)"; fi

parity-check: strategy-audit strategy-fingerprint

push-config:
	@$(PYTHON) scripts/tools/push_config.py --payload $(PARAMS_PATH) --target $(TARGET) --signal-type $(SIGNAL_TYPE)

validate-live-runtime:
	@PYTHONPATH=. $(PYTHON) scripts/tools/validate_live_runtime.py --redis-url $(VALIDATION_REDIS_URL) --strategy-path $(STRATEGY_PATH)

deploy-live:
	@SEP_DEPLOY_STACK=live ./deploy.sh
