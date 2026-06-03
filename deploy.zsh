#!/bin/zsh
set -e

# ── Colors (tput with graceful fallback) ──────────────────────────────────────
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")
BOLD=$(tput bold 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")

# ── Logging helpers ───────────────────────────────────────────────────────────
info()  { echo "${BOLD}${GREEN}[✓]${RESET} $*"; }
step()  { echo "${BOLD}${YELLOW}[→]${RESET} $*"; }
error() { echo "${BOLD}${RED}[✗]${RESET} $*" >&2; }

# ── Config ────────────────────────────────────────────────────────────────────
REMOTE=server
REMOTE_DIR=/data/projs/Screen-Export
DATA_DIR=/var/screen-export-data   # must match SCREEN_EXPORT_DATA_DIR in screen-export.service

# ── State tracking (only roll back resources created by this run) ─────────────
_MODE=${1:-update}
_CLEANUP_DONE=0
_REMOTE_DIR_CREATED=0
_NGINX_DEPLOYED=0
_SERVICE_DEPLOYED=0

# ── Cleanup on error ──────────────────────────────────────────────────────────
cleanup() {
  local exit_code=$?
  [[ $exit_code -eq 0 ]] && return   # success — nothing to clean up
  [[ $_CLEANUP_DONE -eq 1 ]] && return  # prevent reentry
  _CLEANUP_DONE=1

  error "Deploy failed (exit ${exit_code}). Running cleanup..."
  set +e  # remaining cleanup is best-effort

  # Local: remove tarball if it wasn't already deleted
  if [[ -f package.tar.gz ]]; then
    rm -f package.tar.gz
    error "Removed local package.tar.gz"
  fi

  # Remote: remove leftover tarball (non-interactive, short timeout)
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" \
    "rm -f /tmp/package.tar.gz" 2>/dev/null || true

  if [[ $_MODE == "--init" ]]; then
    # Roll back only resources that were created in this run
    if [[ $_SERVICE_DEPLOYED -eq 1 ]]; then
      error "Rolling back systemd service..."
      ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" \
        "systemctl disable --now screen-export.service 2>/dev/null; \
         rm -f /etc/systemd/system/screen-export.service; \
         systemctl daemon-reload" 2>/dev/null || true
    fi

    if [[ $_NGINX_DEPLOYED -eq 1 ]]; then
      error "Rolling back nginx config..."
      ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" \
        "rm -f /etc/nginx/snippets/screen-export/nginx-snippet.conf" 2>/dev/null || true
    fi

    if [[ $_REMOTE_DIR_CREATED -eq 1 ]]; then
      error "Removing remote directory ${REMOTE_DIR}..."
      ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE" \
        "rm -rf ${REMOTE_DIR}" 2>/dev/null || true
    fi
  fi

  error "Cleanup complete. Manual inspection may still be required."
}
trap cleanup EXIT

# ── Package & Transfer ────────────────────────────────────────────────────────
step "Packaging build files..."
tar czf package.tar.gz backend/ deploy/ frontend/ config.py requirements.txt

step "Transferring build to remote server..."
scp package.tar.gz $REMOTE:/tmp/
rm package.tar.gz
info "Build files transferred."

# ── Deploy ────────────────────────────────────────────────────────────────────
if [[ $1 == "--init" ]]; then
  step "Creating remote directory..."
  ssh $REMOTE "mkdir -p $REMOTE_DIR"
  _REMOTE_DIR_CREATED=1

  step "Deploying build files..."
  ssh $REMOTE "tar xzf /tmp/package.tar.gz -C $REMOTE_DIR && rm /tmp/package.tar.gz"
  ssh $REMOTE "chown -R www-data:www-data $REMOTE_DIR"

  step "Creating data directory..."
  ssh $REMOTE "mkdir -p $DATA_DIR && chown -R www-data:www-data $DATA_DIR"

  step "Setting up Python environment..."
  ssh $REMOTE "apt-get install -y software-properties-common && \
      add-apt-repository -y ppa:deadsnakes/ppa && \
      apt-get update -y && \
      apt-get install -y python3.11 python3.11-venv python3.11-dev && \
      python3.11 -m venv $REMOTE_DIR/.venv && \
      zsh -i -c '$REMOTE_DIR/.venv/bin/pip install --upgrade pip && \
                   $REMOTE_DIR/.venv/bin/pip install -r $REMOTE_DIR/requirements.txt'"
  info "Python environment set up."

  step "Setting up nginx configuration..."
  ssh $REMOTE "mkdir -p /etc/nginx/snippets/screen-export"
  scp deploy/nginx-snippet.conf $REMOTE:/etc/nginx/snippets/screen-export/nginx-snippet.conf
  _NGINX_DEPLOYED=1
  ssh $REMOTE "zsh -i -c 'chmod 644 /etc/nginx/snippets/screen-export/nginx-snippet.conf && nginx -t && rlnginx'"
  info "Nginx configured."

  step "Setting up systemd service..."
  scp deploy/screen-export.service $REMOTE:/etc/systemd/system/screen-export.service
  _SERVICE_DEPLOYED=1
  ssh $REMOTE "zsh -i -c 'sysdr && systemctl enable --now screen-export.service'"
  info "Systemd service configured and started."
else
  step "Deploying build files..."
  ssh $REMOTE "tar xzf /tmp/package.tar.gz -C $REMOTE_DIR && rm /tmp/package.tar.gz"
  ssh $REMOTE "chown -R www-data:www-data $REMOTE_DIR"
fi

info "Deployment complete. Your website should now be live."
