#!/bin/bash
# SEP Engine Production Deployment Script
# Simplified version focusing on core deployment functionality

set -e

# Colors and logging
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() { echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }

usage() {
    cat <<'EOF'
Usage: ./deploy.sh [--stack <live|full>] [--compose <file>] [--help]

Defaults:
  --stack live    Deploy the canonical droplet stack.

Examples:
  ./deploy.sh
  ./deploy.sh --stack full
  ./deploy.sh --compose docker-compose.live.yml
EOF
}

require_droplet_role() {
    local node_role="${SEP_NODE_ROLE:-unknown}"
    local override="${SEP_ALLOW_NON_DROPLET_DEPLOY:-0}"

    if [[ "$override" == "1" ]]; then
        warning "Bypassing node-role deploy guard because SEP_ALLOW_NON_DROPLET_DEPLOY=1"
        return 0
    fi

    if [[ "$node_role" != "droplet" ]]; then
        error "Refusing deployment from node role '${node_role}'."
        error "This script is droplet-only. Set SEP_NODE_ROLE=droplet on the live host."
        error "Research/GPU nodes should keep SEP_NODE_ROLE=gpu."
        error "Use SEP_ALLOW_NON_DROPLET_DEPLOY=1 only for an explicit one-off override."
        exit 1
    fi
}

# Configuration
PROJECT_NAME="sep"
MODE=""
COMPOSE_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stack)
            if [[ $# -lt 2 ]]; then
                error "--stack requires a value"
                usage
                exit 1
            fi
            MODE="$2"
            shift 2
            ;;
        --compose)
            if [[ $# -lt 2 ]]; then
                error "--compose requires a file path"
                usage
                exit 1
            fi
            COMPOSE_FILE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$MODE" ]]; then
    MODE="${SEP_DEPLOY_STACK:-live}"
fi

if [[ -z "$COMPOSE_FILE" ]]; then
    COMPOSE_FILE="docker-compose.${MODE}.yml"
elif [[ -z "${SEP_DEPLOY_STACK:-}" ]]; then
    MODE="$(basename "$COMPOSE_FILE" .yml)"
    MODE="${MODE#docker-compose.}"
fi

# Load non-secret environment files in order of precedence
load_base_env() {
    # Load base .env if exists
    if [[ -f ".env" ]]; then
        log "Loading .env file"
        set -a
        source .env
        set +a
    fi
    
    # Load mode-specific env first (before OANDA credentials)
    if [[ -f ".env.${MODE}" ]]; then
        log "Loading .env.${MODE} file"
        set -a
        source .env.${MODE}
        set +a
    fi
}

# Load OANDA credentials only after the node-role guard passes
load_oanda_env() {
    # Load OANDA credentials last so they override any empty values
    if [[ -f "OANDA.env" ]]; then
        log "Loading OANDA.env file"
        set -a
        source OANDA.env
        set +a
    elif [[ -f "config/OANDA.env" ]]; then
        log "Loading config/OANDA.env file"
        set -a
        source config/OANDA.env
        set +a
    else
        log "No OANDA.env file found in current directory or config/"
    fi
}

load_base_env
require_droplet_role

# Check Docker installation and use appropriate command
if command -v docker compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
else
    error "Docker Compose not found. Please install Docker with Compose plugin."
    exit 1
fi

# Check if compose file exists
if [[ ! -f "$COMPOSE_FILE" ]]; then
    error "Compose file $COMPOSE_FILE not found"
    exit 1
fi

log "Starting SEP Engine deployment in $MODE mode"
log "Using compose file: $COMPOSE_FILE"
log "Docker compose command: $DOCKER_COMPOSE"

load_oanda_env

compose_has_service() {
    $DOCKER_COMPOSE -f "$COMPOSE_FILE" config --services 2>/dev/null | grep -Fxq "$1"
}

if ! compose_has_service frontend; then
    warning "Frontend service is not part of $COMPOSE_FILE"
    warning "Use ./deploy.sh --stack full if you need the dashboard, kill switch UI, and NAV monitor."
fi

run_parity_checks() {
    local params_path="${PARAMS_PATH:-output/live_params.json}"
    if [[ -f "$params_path" ]]; then
        log "Running strategy parity checks with $params_path..."
        make parity-check PARAMS_PATH="$params_path"
        return 0
    fi

    warning "No params artifact found at $params_path; skipping strategy audit"
    make strategy-fingerprint PARAMS_PATH="$params_path"
}

run_runtime_validation() {
    local redis_url="${VALKEY_URL:-redis://valkey:6379/0}"
    local strategy_path="${STRATEGY_PROFILE:-/app/config/mean_reversion_strategy.yaml}"
    local instruments_csv="${HOTBAND_PAIRS:-}"
    local -a args=(
        env PYTHONPATH=/app python /app/scripts/tools/validate_live_runtime.py
        --redis-url "$redis_url"
        --strategy-path "$strategy_path"
    )

    if [[ -n "$instruments_csv" ]]; then
        IFS=',' read -r -a validation_instruments <<< "$instruments_csv"
        args+=(--instruments "${validation_instruments[@]}")
    fi

    log "Validating live runtime feeds and gate payloads..."
    $DOCKER_COMPOSE -f "$COMPOSE_FILE" exec -T backend "${args[@]}"
}

# Validate required OANDA credentials
if [[ -n "${OANDA_API_KEY:-}" ]]; then
    oanda_key_state="set"
else
    oanda_key_state="not_set"
fi
if [[ -n "${OANDA_ACCOUNT_ID:-}" ]]; then
    oanda_account_state="set"
else
    oanda_account_state="not_set"
fi
log "Validating credentials: OANDA_API_KEY=${oanda_key_state}, OANDA_ACCOUNT_ID=${oanda_account_state}"
if [[ -z "$OANDA_ACCOUNT_ID" ]] || [[ -z "$OANDA_API_KEY" ]]; then
    error "OANDA credentials not found. Please ensure OANDA.env file exists with:"
    error "  OANDA_ACCOUNT_ID=your_account_id"
    error "  OANDA_API_KEY=your_api_key"
    if [[ -f "OANDA.env" ]]; then
        error "OANDA.env exists but credentials are not being loaded properly"
        if grep -Eq "OANDA_API_KEY=|OANDA_ACCOUNT_ID=" OANDA.env; then
            error "OANDA.env contains credential keys but one or more values resolved empty"
        else
            error "No OANDA credential keys found in OANDA.env"
        fi
    fi
    exit 1
fi

# Set default retention values if not set
export VALKEY_SIGNAL_RETENTION=${VALKEY_SIGNAL_RETENTION:-200000}
export VALKEY_CANDLE_RETENTION=${VALKEY_CANDLE_RETENTION:-0}

# Get HOTBAND_PAIRS from Python if available, otherwise use default
if [[ -z "$HOTBAND_PAIRS" ]]; then
    HOTBAND_PAIRS="EUR_USD,USD_JPY,AUD_USD,USD_CHF,NZD_USD,GBP_USD,USD_CAD"
fi
export HOTBAND_PAIRS

log "HOTBAND_PAIRS: $HOTBAND_PAIRS"

run_parity_checks

# Stop existing services
log "Stopping existing services..."
$DOCKER_COMPOSE -f "$COMPOSE_FILE" down --remove-orphans || true

# Pull latest images
log "Pulling latest images..."
$DOCKER_COMPOSE -f "$COMPOSE_FILE" pull || warning "Pull failed, continuing with local images"

# Build services
log "Building services..."
$DOCKER_COMPOSE -f "$COMPOSE_FILE" build

# Start services
log "Starting services..."
$DOCKER_COMPOSE -f "$COMPOSE_FILE" up -d

# Seed Valkey defaults (kill switch, risk snapshot)
log "Seeding Valkey defaults..."
if ! $DOCKER_COMPOSE -f "$COMPOSE_FILE" exec -T backend python /app/scripts/tools/seed_valkey_defaults.py; then
    warning "Unable to seed Valkey defaults"
fi

# Wait for services to start
log "Waiting for services to initialize..."
sleep 20

# Health check function
health_check() {
    local backend_url="http://localhost:8000"
    
    log "Checking backend health at $backend_url/health..."
    if curl -sf "$backend_url/health" >/dev/null 2>&1; then
        success "Backend health check passed"
    else
        error "Backend health check failed"
        return 1
    fi

    if compose_has_service frontend; then
        local frontend_url="http://localhost/health"
        log "Checking frontend health at $frontend_url..."
        if curl -sf "$frontend_url" >/dev/null 2>&1; then
            success "Frontend health check passed"
            return 0
        else
            error "Frontend health check failed"
            return 1
        fi
    fi

    log "Frontend service not present in this compose stack; skipping frontend health check"
    return 0
}

# Retry health check
MAX_RETRIES=5
RETRY_DELAY=10

for i in $(seq 1 $MAX_RETRIES); do
    if health_check; then
        break
    elif [[ $i -lt $MAX_RETRIES ]]; then
        warning "Health check failed (attempt $i/$MAX_RETRIES), retrying in $RETRY_DELAY seconds..."
        sleep $RETRY_DELAY
    else
        error "Health check failed after $MAX_RETRIES attempts"
        log "Showing container logs for debugging:"
        $DOCKER_COMPOSE -f "$COMPOSE_FILE" logs --tail=50
        log "Container status:"
        $DOCKER_COMPOSE -f "$COMPOSE_FILE" ps
        exit 1
    fi
done

run_runtime_validation

# Show final status
log "Deployment status:"
$DOCKER_COMPOSE -f "$COMPOSE_FILE" ps

success "SEP Engine deployment completed successfully!"
log ""
log "Services running:"
log "  Backend API: http://localhost:8000"
if compose_has_service frontend; then
    log "  Frontend UI:  https://mxbikes.xyz"
fi
log ""
log "Useful commands:"
log "  View logs:    $DOCKER_COMPOSE -f $COMPOSE_FILE logs -f"
log "  Stop:         $DOCKER_COMPOSE -f $COMPOSE_FILE down"
log "  Restart:      $DOCKER_COMPOSE -f $COMPOSE_FILE restart"
log ""
log "Additional operations via make:"
log "  make lint                  - Syntax check Python modules"
log "  make build-manifold-engine - Rebuild the native Python extension"
log "  make strategy-audit        - Verify YAML/profile parity before promotion"
