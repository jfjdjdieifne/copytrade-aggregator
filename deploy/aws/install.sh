#!/usr/bin/env bash
# Install the radar as a systemd service on an EC2 instance.
#
# Deliberately small and readable: this runs as root on the operator's own box,
# so every line is one any operator can audit before trusting it.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
PYTHON="${PYTHON:-$(command -v python3.11 || command -v python3)}"
INTERVAL="${INTERVAL:-45}"

echo "→ repo:   ${REPO_DIR}"
echo "→ user:   ${SERVICE_USER}"
echo "→ python: ${PYTHON}"
echo "→ interval: ${INTERVAL}s"

if [[ ! -f "${REPO_DIR}/entry.py" ]]; then
  echo "✗ ${REPO_DIR} does not look like the repository root" >&2
  exit 1
fi

# The service reads its configuration from .env. Refusing to start without it is
# better than starting with empty Telegram credentials and looking dead.
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  echo "✗ ${REPO_DIR}/.env is missing — copy .env.example and fill it in first" >&2
  exit 1
fi

UNIT=/etc/systemd/system/copytrade.service
sed -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__USER__|${SERVICE_USER}|g" \
    -e "s|__PYTHON__|${PYTHON}|g" \
    -e "s|__INTERVAL__|${INTERVAL}|g" \
    "${REPO_DIR}/deploy/aws/copytrade.service" > /tmp/copytrade.service

install -m 644 /tmp/copytrade.service "${UNIT}"
rm -f /tmp/copytrade.service

systemctl daemon-reload
systemctl enable --now copytrade
systemctl status copytrade --no-pager || true

cat <<EOF

✔ installed.

  تابع اللوج:   sudo journalctl -u copytrade -f
  أوقف:        sudo systemctl stop copytrade
  أعد التشغيل: sudo systemctl restart copytrade

  تذكّر: هاد النظام بيُراقب ويُبلغ. التنفيذ بمال حقيقي مقفول افتراضياً
  ومش مفتوح بهاذا السكربت — بيلزم قرار صريح منك بعد إثبات ورقي.
EOF
