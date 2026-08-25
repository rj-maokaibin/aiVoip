#!/usr/bin/env bash
set -euo pipefail

ROOT="${VOIP_ACCEPTANCE_ROOT:-/opt/voip-acceptance}"
RUNNER_USER="${VOIP_ACCEPTANCE_RUNNER_USER:-github-runner}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GOLDEN_SOURCE="${VOIP_GOLDEN_001_SOURCE:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "BOOTSTRAP_REQUIRES_ROOT: run with sudo" >&2
  exit 2
fi

id "$RUNNER_USER" >/dev/null 2>&1 || {
  echo "RUNNER_USER_NOT_FOUND:$RUNNER_USER" >&2
  exit 2
}

install -d -m 0755 "$ROOT"
for d in golden-cache runtime/bin runtime/tshark-4.2.2 state logs work; do
  install -d -o "$RUNNER_USER" -g "$RUNNER_USER" -m 0755 "$ROOT/$d"
done

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$RUNNER_USER"
fi

if [ -z "$GOLDEN_SOURCE" ] && [ -r /tmp/tcpdump-2026-08-14.pcap ]; then
  # One-time migration compatibility only. Normal CI never reads /tmp.
  GOLDEN_SOURCE=/tmp/tcpdump-2026-08-14.pcap
fi

if [ -n "$GOLDEN_SOURCE" ]; then
  sudo -u "$RUNNER_USER" -H env VOIP_ACCEPTANCE_ROOT="$ROOT" \
    python3 "$REPO_ROOT/tools/acceptance_golden.py" ensure --source "$GOLDEN_SOURCE"
else
  sudo -u "$RUNNER_USER" -H env VOIP_ACCEPTANCE_ROOT="$ROOT" \
    python3 "$REPO_ROOT/tools/acceptance_golden.py" verify || {
      echo "GOLDEN_SOURCE_REQUIRED: set VOIP_GOLDEN_001_SOURCE once" >&2
      exit 2
    }
fi

TSHARK_WRAPPER="$ROOT/runtime/bin/tshark"
if command -v tshark >/dev/null 2>&1 && tshark -v | head -n1 | grep -q '4.2.2'; then
  SYSTEM_TSHARK="$(command -v tshark)"
  cat >"$TSHARK_WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$SYSTEM_TSHARK" "\$@"
EOF
  chmod 0755 "$TSHARK_WRAPPER"
else
  apt-get update
  tmp_debs="$(mktemp -d)"
  extract_root="$ROOT/runtime/tshark-4.2.2/root"
  rm -rf "$extract_root"
  mkdir -p "$extract_root"
  (
    cd "$tmp_debs"
    mapfile -t pkgs < <(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances tshark | awk '/^[^ <][^:]*$/ {print $1}' | sort -u)
    apt-get download "${pkgs[@]}"
    shopt -s nullglob
    for deb in ./*.deb; do
      dpkg-deb -x "$deb" "$extract_root"
    done
  )
  rm -rf "$tmp_debs"
  cat >"$TSHARK_WRAPPER" <<EOF
#!/usr/bin/env bash
export LD_LIBRARY_PATH="$extract_root/usr/lib/x86_64-linux-gnu:$extract_root/lib/x86_64-linux-gnu:\${LD_LIBRARY_PATH:-}"
export WIRESHARK_DATA_DIR="$extract_root/usr/share/wireshark"
exec "$extract_root/usr/bin/tshark" "\$@"
EOF
  chmod 0755 "$TSHARK_WRAPPER"
fi

"$TSHARK_WRAPPER" -v | head -n1 | grep -q '4.2.2' || {
  echo "TSHARK_4_2_2_BOOTSTRAP_FAILED" >&2
  exit 2
}

docker build \
  -t voip-acceptance-runtime:v2.0.0 \
  -f "$REPO_ROOT/deploy/acceptance_v2/Dockerfile" \
  "$REPO_ROOT"

python3 "$REPO_ROOT/tools/acceptance_stack.py" up

chown -R "$RUNNER_USER:$RUNNER_USER" "$ROOT"

sudo -u "$RUNNER_USER" -H env VOIP_ACCEPTANCE_ROOT="$ROOT" \
  python3 "$REPO_ROOT/tools/acceptance_runner_doctor.py" \
    --require-network --deep-network \
    --require-docker --require-golden --require-tshark --require-stack --repair

echo "VOIP_ACCEPTANCE_BOOTSTRAP=PASS"
echo "NOTE: docker group membership may require restarting the github-runner service/session."
