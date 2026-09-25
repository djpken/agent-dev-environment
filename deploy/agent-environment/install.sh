#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

source_root=
public_host=

usage() {
  cat >&2 <<'EOF'
Usage: install.sh --source-root /absolute/path/to/ade --public-host <ip-or-hostname>
EOF
}

while (($#)); do
  case "$1" in
    --source-root)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      source_root=$2
      shift 2
      ;;
    --public-host)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      public_host=$2
      shift 2
      ;;
    --help|-h)
      usage >&2
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ $(id -u) == 0 ]] || { echo 'install.sh must run as root' >&2; exit 1; }
[[ -n "$source_root" && "$source_root" == /* ]] || { echo '--source-root must be absolute' >&2; exit 2; }
source_root=$(cd "$source_root" && pwd -P)
[[ -f "$source_root/go.mod" && -f "$source_root/cmd/ade/main.go" ]] || {
  echo 'Go CLI sources or go.mod are missing from source root' >&2
  exit 1
}
[[ -f "$source_root/web/package-lock.json" && -f "$source_root/web/src/client/main.tsx" ]] || {
  echo 'dashboard sources or package lock are missing from source root' >&2
  exit 1
}
[[ "$public_host" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || { echo '--public-host contains unsupported characters' >&2; exit 2; }

node_bin=$(command -v node || true)
npm_bin=$(command -v npm || true)
[[ -n "$node_bin" && -n "$npm_bin" ]] || {
  echo 'Node.js 22.12+ and npm are required to install ADES' >&2
  exit 1
}
node_version=$($node_bin --version)
node_version=${node_version#v}
IFS=. read -r node_major node_minor _ <<<"$node_version"
if ((node_major < 22 || (node_major == 22 && node_minor < 12))); then
  echo "Node.js 22.12+ is required; found $node_version" >&2
  exit 1
fi

go_bin=$(command -v go || true)
if [[ -z "$go_bin" && -x /usr/local/go/bin/go ]]; then
  go_bin=/usr/local/go/bin/go
fi
[[ -n "$go_bin" && -x "$go_bin" ]] || {
  echo 'Go 1.27.1 is required to build the ADES binary' >&2
  exit 1
}
go_version=$($go_bin env GOVERSION)
[[ "$go_version" == go1.27.1 ]] || {
  echo "Go 1.27.1 is required; found $go_version" >&2
  exit 1
}

# Build the React dashboard and the standalone Go CLI/API binary.
"$npm_bin" ci --prefix "$source_root/web"
"$npm_bin" run build --prefix "$source_root/web"
[[ -f "$source_root/web/dist/ui/index.html" ]] || {
  echo 'React dashboard build did not produce its UI bundle' >&2
  exit 1
}
temporary_ade=$(mktemp /tmp/ade-binary.XXXXXX)
trap 'rm -f -- "$temporary_ade"' EXIT
(
  cd "$source_root"
  GOTOOLCHAIN=local CGO_ENABLED=0 "$go_bin" build -trimpath -ldflags='-s -w' -o "$temporary_ade" ./cmd/ade
)
install -o root -g root -m 0755 "$temporary_ade" /usr/local/bin/ade

install -d -o root -g root -m 0755 /etc/ade /etc/ade/tls
install -d -o root -g orca -m 0770 /var/lib/ade/agent-environment
# The unprivileged dashboard and CLI share the Nginx artifact directory.
if [[ ! -e /var/lib/ade/web-artifacts ]]; then
  install -d -o orca -g orca -m 0755 /var/lib/ade/web-artifacts
fi

source_template=$source_root/deploy/agent-environment/agent-environment.env.example
config_template=$source_root/deploy/agent-environment/agent-environment.json.example
[[ -f "$source_template" && -f "$config_template" ]] || {
  echo 'agent-environment deployment templates are missing from source root' >&2
  exit 1
}

escaped_source_root=$(printf '%s' "$source_root" | sed 's/[\\&|]/\\&/g')
temporary_env=$(mktemp /tmp/agent-environment.env.XXXXXX)
temporary_config=$(mktemp /tmp/agent-environment.json.XXXXXX)
trap 'rm -f -- "$temporary_ade" "$temporary_env" "$temporary_config"' EXIT
sed \
  -e "s|__SOURCE_ROOT__|$escaped_source_root|g" \
  "$source_template" >"$temporary_env"
install -o root -g root -m 0644 "$temporary_env" /etc/ade/agent-environment.env

if [[ ! -f /etc/ade/agent-environment.json ]]; then
  vm_id=$(cat /proc/sys/kernel/random/uuid)
  sed \
    -e "s|__VM_ID__|$vm_id|g" \
    -e "s|__SOURCE_ROOT__|$escaped_source_root|g" \
    -e "s|__PUBLIC_HOST__|$public_host|g" \
    "$config_template" >"$temporary_config"
  install -o root -g orca -m 0640 "$temporary_config" /etc/ade/agent-environment.json
fi

if [[ ! -f /etc/ade/tls/agent-environment.crt || ! -f /etc/ade/tls/agent-environment.key ]]; then
  if [[ "$public_host" == *:* || "$public_host" =~ ^[0-9.]+$ ]]; then
    san="IP:$public_host"
  else
    san="DNS:$public_host"
  fi
  temporary_key=$(mktemp /tmp/agent-environment.key.XXXXXX)
  temporary_cert=$(mktemp /tmp/agent-environment.crt.XXXXXX)
  trap 'rm -f -- "$temporary_ade" "$temporary_env" "$temporary_config" "$temporary_key" "$temporary_cert"' EXIT
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
    -keyout "$temporary_key" -out "$temporary_cert" \
    -subj "/CN=$public_host" -addext "subjectAltName=$san" >/dev/null 2>&1
  install -o root -g orca -m 0640 "$temporary_key" /etc/ade/tls/agent-environment.key
  install -o root -g orca -m 0644 "$temporary_cert" /etc/ade/tls/agent-environment.crt
  rm -f -- "$temporary_key" "$temporary_cert"
fi

install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/ade-environment-service.wrapper" \
  /usr/local/bin/ade-environment-service
install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/ade-environment-update.wrapper" \
  /usr/local/sbin/agent-environment-update
install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/agent-environment-trigger" \
  /usr/local/sbin/agent-environment-trigger
install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/agent-environment-authorize" \
  /usr/local/sbin/agent-environment-authorize
install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/codex-headless-update" \
  /usr/local/sbin/codex-headless-update
install -o root -g root -m 0755 \
  "$source_root/deploy/agent-environment/orca-headless-update" \
  /usr/local/sbin/orca-headless-update
install -o root -g root -m 0644 \
  "$source_root/deploy/agent-environment/agent-environment.service" \
  /etc/systemd/system/agent-environment.service
install -o root -g root -m 0644 \
  "$source_root/deploy/agent-environment/agent-environment-update.service" \
  /etc/systemd/system/agent-environment-update.service
install -o root -g root -m 0644 \
  "$source_root/deploy/agent-environment/agent-environment-update.timer" \
  /etc/systemd/system/agent-environment-update.timer
install -o root -g root -m 0440 \
  "$source_root/deploy/agent-environment/agent-environment.sudoers" \
  /etc/sudoers.d/agent-environment

visudo -cf /etc/sudoers.d/agent-environment >/dev/null
systemctl daemon-reload
if systemctl is-enabled --quiet orca-headless-update.timer 2>/dev/null; then
  systemctl disable --now orca-headless-update.timer
fi
systemctl enable --now agent-environment.service
systemctl enable --now agent-environment-update.timer

echo "Agent environment service installed for $public_host:6790"
echo "Schedule: daily 04:00 UTC+8 (20:00 UTC)"
