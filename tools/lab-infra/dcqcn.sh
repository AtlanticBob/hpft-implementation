#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./dcqcn.sh <interface> [name=value ...]

Examples:
  ./dcqcn.sh bf0_1
  ./dcqcn.sh bf0_1 rpg_ai_rate=10
  ./dcqcn.sh bf0_1 rpg_ai_rate=10 min_time_between_cnps=2
  sudo ./dcqcn.sh bf0_1 roce_rp.enable.5=1 roce_np.enable.5=1

Notes:
  - Reads/writes: /sys/class/net/<interface>/ecn
  - Parameter names are auto-detected under roce_rp and roce_np.
  - Use <protocol>.<name>=<value> if a name is ambiguous.
  - Use <protocol>.enable.<priority>=<0|1> for per-priority enable files.
  - On this host, DCQCN/ECN knobs are exposed on the PF netdev, for example bf0_1.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

read_value() {
  local path="$1"

  if [[ ! -r "$path" ]]; then
    printf '<no read permission>'
    return 0
  fi

  local value
  value="$(cat "$path" 2>/dev/null || true)"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  printf '%s' "$value"
}

write_value() {
  local path="$1"
  local value="$2"

  if [[ ! -e "$path" ]]; then
    die "sysfs file does not exist: $path"
  fi
  if [[ ! -w "$path" ]]; then
    die "no write permission: $path
Tip: try running with sudo."
  fi

  printf '%s\n' "$value" >"$path"
}

print_enable() {
  local enable_dir="$1"
  local prio value

  if [[ ! -d "$enable_dir" ]]; then
    echo "  enable per priority  <none>"
    return 0
  fi

  printf '  enable per priority '
  for prio in 0 1 2 3 4 5 6 7; do
    if [[ -f "$enable_dir/$prio" ]]; then
      value="$(read_value "$enable_dir/$prio")"
      printf ' %s:%s' "$prio" "$value"
    fi
  done
  printf '\n'
}

print_params() {
  local protocol_dir="$1"
  local files=()
  local file name value max_len=0

  while IFS= read -r -d '' file; do
    files+=("$file")
    name="$(basename "$file")"
    (( ${#name} > max_len )) && max_len="${#name}"
  done < <(find "$protocol_dir" -maxdepth 1 -type f -print0 | sort -z)

  echo "  parameters:"
  if (( ${#files[@]} == 0 )); then
    echo "  <none>"
    return 0
  fi

  for file in "${files[@]}"; do
    name="$(basename "$file")"
    value="$(read_value "$file")"
    printf "  %-*s  %s\n" "$max_len" "$name" "$value"
  done
}

show_protocol() {
  local ecn_dir="$1"
  local protocol="$2"
  local protocol_dir="$ecn_dir/$protocol"

  [[ -d "$protocol_dir" ]] || return 0

  echo "[$protocol]"
  print_enable "$protocol_dir/enable"
  print_params "$protocol_dir"
  echo
}

find_param_path() {
  local ecn_dir="$1"
  local name="$2"
  local matches=()
  local protocol path

  if [[ "$name" == *.* ]]; then
    protocol="${name%%.*}"
    name="${name#*.}"
    path="$ecn_dir/$protocol/$name"
    [[ -f "$path" ]] || die "parameter not found: $protocol.$name"
    printf '%s' "$path"
    return 0
  fi

  for protocol in roce_rp roce_np; do
    path="$ecn_dir/$protocol/$name"
    [[ -f "$path" ]] && matches+=("$path")
  done

  if (( ${#matches[@]} == 0 )); then
    die "parameter not found: $name"
  fi
  if (( ${#matches[@]} > 1 )); then
    die "ambiguous parameter: $name
Tip: specify protocol explicitly, for example roce_rp.$name or roce_np.$name."
  fi

  printf '%s' "${matches[0]}"
}

find_enable_path() {
  local ecn_dir="$1"
  local name="$2"
  local protocol rest prio path

  protocol="${name%%.*}"
  rest="${name#*.}"

  [[ "$protocol" == "roce_rp" || "$protocol" == "roce_np" ]] ||
    die "enable assignment must start with roce_rp or roce_np: $name"
  [[ "$rest" == enable.* ]] ||
    die "invalid enable assignment: $name"

  prio="${rest#enable.}"
  [[ "$prio" =~ ^[0-7]$ ]] || die "priority must be 0..7: $name"

  path="$ecn_dir/$protocol/enable/$prio"
  [[ -f "$path" ]] || die "enable file not found: $path"
  printf '%s' "$path"
}

set_assignment() {
  local ecn_dir="$1"
  local assignment="$2"
  local name value path old new

  [[ "$assignment" == *=* ]] || die "invalid assignment, expected name=value: $assignment"
  name="${assignment%%=*}"
  value="${assignment#*=}"

  [[ -n "$name" ]] || die "empty parameter name in assignment: $assignment"
  [[ -n "$value" ]] || die "empty value in assignment: $assignment"

  if [[ "$name" == *.enable.* ]]; then
    path="$(find_enable_path "$ecn_dir" "$name")"
  else
    path="$(find_param_path "$ecn_dir" "$name")"
  fi

  old="$(read_value "$path")"
  write_value "$path" "$value"
  new="$(read_value "$path")"

  printf '%s  %s -> %s\n' "${path#"$ecn_dir/"}" "${old:-<empty>}" "${new:-<empty>}"
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "$#" -lt 1 ]]; then
    usage
    [[ "$#" -eq 1 ]] && exit 0 || exit 2
  fi

  local iface="$1"
  local netdev_dir="/sys/class/net/$iface"
  local ecn_dir="$netdev_dir/ecn"
  shift

  [[ -d "$netdev_dir" ]] || die "interface not found: $iface"
  [[ -d "$ecn_dir" ]] || die "$iface has no ECN/DCQCN sysfs directory: $ecn_dir
Tip: on this host the ECN/DCQCN knobs are usually exposed on the PF netdev, for example bf0_1, not on VF netdevs."

  echo "Interface : $iface"
  echo "ECN path  : $ecn_dir"
  echo

  if (( "$#" > 0 )); then
    local assignment
    for assignment in "$@"; do
      set_assignment "$ecn_dir" "$assignment"
    done
    echo
  fi

  show_protocol "$ecn_dir" "roce_rp"
  show_protocol "$ecn_dir" "roce_np"
}

main "$@"
