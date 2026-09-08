"""Shared helpers for the HPFT TCP EDT/BPF shaper tools."""

from __future__ import annotations

import ipaddress
import ctypes
import json
import os
import platform
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


TCP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TCP_ROOT.parents[1]
DEFAULT_EGRESS_DEV = "p1"
DEFAULT_PIN_DIR = Path("/sys/fs/bpf/hpft_tcp_edt")
DEFAULT_SECTION = "classifier"
# THE authoritative TCP executor source. This used to default to a copy
# vendored under tools/tcp_shaper/bpf/, which was a different program -
# no sparse-bypass conditions, no wire-byte metering. Re-applying without
# an explicit --bpf-object therefore swapped the executor silently, and
# the swap is invisible at every layer that reports health. One source of
# truth now; the copies are deleted.
DEFAULT_BPF_SOURCE = REPO_ROOT / "tcp" / "bpf-opt3" / "hpft_tcp_edt_kern.c"
DEFAULT_BPF_OBJECT = REPO_ROOT / "tcp" / "bpf-opt3" / "hpft_tcp_edt_kern.o"
DEFAULT_RULES_SECTION = "tcp_rules"
DEFAULT_QDISC_MODE = "mq-leaf-fq"
DEFAULT_MQ_LEAF_COUNT = 4
BPF_ANY = 0
BPF_MAP_UPDATE_ELEM = 2
BPF_OBJ_GET = 7


class TcpShaperError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    purpose: str
    argv: list[str]
    ignore_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"purpose": self.purpose, "argv": self.argv, "ignore_error": self.ignore_error}


@dataclass(frozen=True)
class MapUpdate:
    map_name: str
    key: bytes
    value: bytes
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "map": self.map_name,
            "key_hex": bytes_to_hex_list(self.key),
            "value_hex": bytes_to_hex_list(self.value),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ApplyResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def load_registry(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        registry = json.load(handle)
    if not isinstance(registry, dict):
        raise TcpShaperError("registry must be a JSON object")
    if "vnics" not in registry or not isinstance(registry["vnics"], list):
        raise TcpShaperError("registry must contain a vnics list")
    return registry


def select_rules(registry: dict[str, Any], section: str) -> list[dict[str, Any]]:
    if section == "auto":
        if registry.get("tcp_rules"):
            section = "tcp_rules"
        elif registry.get("rdma_rules"):
            section = "rdma_rules"
        else:
            raise TcpShaperError("registry has neither tcp_rules nor rdma_rules")
    rules = registry.get(section)
    if not isinstance(rules, list):
        raise TcpShaperError(f"registry section {section!r} is not a list")
    return rules


def vnic_index(registry: dict[str, Any]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for idx, vnic in enumerate(registry["vnics"], start=1):
        vnic_id = str(vnic.get("vnic_id", ""))
        if not vnic_id:
            raise TcpShaperError("vnic entry missing vnic_id")
        if vnic_id in indices:
            raise TcpShaperError(f"duplicate vnic_id: {vnic_id}")
        indices[vnic_id] = idx
    return indices


def vnic_by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for vnic in registry["vnics"]:
        result[str(vnic["vnic_id"])] = vnic
    return result


def local_host(registry: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    host = registry.get("local_host")
    if not host:
        raise TcpShaperError("local host is not set; pass --local-host")
    return str(host)


def parse_ipv4(value: str) -> bytes:
    addr = ipaddress.ip_address(value)
    if addr.version != 4:
        raise TcpShaperError(f"only IPv4 is supported in this prototype: {value}")
    return addr.packed


def bytes_to_hex_list(data: bytes) -> list[str]:
    return [f"{byte:02x}" for byte in data]


def bpftool_hex_arg(data: bytes) -> list[str]:
    return ["hex", *bytes_to_hex_list(data)]


def bpftool_bin() -> str:
    return os.environ.get("HPFT_BPFTOOL", "bpftool")


def set_bpftool_bin(path: str | None) -> None:
    if path:
        os.environ["HPFT_BPFTOOL"] = path


def tc_handle_id(value: int) -> str:
    return format(value, "x")


def pack_u32(value: int) -> bytes:
    return struct.pack("<I", value)


def pack_u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def pair_key(src_idx: int, dst_idx: int) -> int:
    return (src_idx << 32) | dst_idx


def pack_rate_cfg(rate_bps: int, generation: int, burst_bytes: int, flags: int = 0) -> bytes:
    return struct.pack("<QQII", rate_bps, generation, burst_bytes, flags)


def pack_pair_state(next_ns: int = 0, generation: int = 0) -> bytes:
    """struct hpft_pair_state (per-connection executor, 2026-09-07): lock,
    epoch, epoch_ns, sum_cur, sum_prev, n_cur, n_prev, generation, shots,
    nosock, reserved. Every field means "nothing seen yet" at zero; the
    clocks now live per connection, so next_ns is accepted for the old
    callers and ignored."""
    del next_ns
    return struct.pack("<IIQQQIIQIIQ", 0, 0, 0, 0, 0, 0, 0, generation, 0, 0, 0)


def normalize_positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TcpShaperError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise TcpShaperError(f"{field} must be > 0")
    return parsed


def resolve_ifindex(netdev: str) -> int | None:
    try:
        return socket.if_nametoindex(netdev)
    except OSError:
        return None


def iter_vnic_ips(vnic: dict[str, Any]) -> Iterable[str]:
    ips = vnic.get("ips", [])
    if not isinstance(ips, list):
        return []
    return (str(ip) for ip in ips)


def iter_vnic_netdevs(vnic: dict[str, Any]) -> Iterable[str]:
    netdevs = vnic.get("netdevs", [])
    if not isinstance(netdevs, list):
        return []
    return (str(netdev) for netdev in netdevs)


def map_paths(pin_dir: Path) -> dict[str, Path]:
    maps = pin_dir / "maps"
    return {
        "hpft_ifindex_to_vnic": maps / "hpft_ifindex_to_vnic",
        "hpft_ip_to_vnic": maps / "hpft_ip_to_vnic",
        "hpft_pair_cfg": maps / "hpft_pair_cfg",
        "hpft_pair_state": maps / "hpft_pair_state",
    }


def build_map_updates(
    registry: dict[str, Any],
    rules: list[dict[str, Any]],
    local_host_name: str,
    generation: int,
    resolve_live_ifindex: bool,
) -> list[MapUpdate]:
    indices = vnic_index(registry)
    by_id = vnic_by_id(registry)
    updates: list[MapUpdate] = []
    seen_ip_keys: set[bytes] = set()
    seen_ifindex_keys: set[int] = set()

    for vnic in registry["vnics"]:
        vnic_id = str(vnic["vnic_id"])
        idx = indices[vnic_id]
        for ip in iter_vnic_ips(vnic):
            key = parse_ipv4(ip)
            if key in seen_ip_keys:
                continue
            seen_ip_keys.add(key)
            updates.append(
                MapUpdate(
                    "hpft_ip_to_vnic",
                    key,
                    pack_u32(idx),
                    {"ip": ip, "vnic_id": vnic_id, "vnic_idx": idx},
                )
            )

        if str(vnic.get("host", "")) != local_host_name:
            continue
        for netdev in iter_vnic_netdevs(vnic):
            if not resolve_live_ifindex:
                continue
            ifindex = resolve_ifindex(netdev)
            if ifindex is None:
                continue
            if ifindex in seen_ifindex_keys:
                continue
            seen_ifindex_keys.add(ifindex)
            updates.append(
                MapUpdate(
                    "hpft_ifindex_to_vnic",
                    pack_u32(ifindex),
                    pack_u32(idx),
                    {
                        "netdev": netdev,
                        "ifindex": ifindex,
                        "vnic_id": vnic_id,
                        "vnic_idx": idx,
                        "status": "resolved",
                    },
                )
            )

    for rule in rules:
        src_vnic = str(rule.get("src_vnic", ""))
        dst_vnic = str(rule.get("dst_vnic", ""))
        if src_vnic not in indices:
            raise TcpShaperError(f"rule src_vnic not found: {src_vnic}")
        if dst_vnic not in indices:
            raise TcpShaperError(f"rule dst_vnic not found: {dst_vnic}")
        if src_vnic not in by_id or str(by_id[src_vnic].get("host", "")) != local_host_name:
            continue

        src_idx = indices[src_vnic]
        dst_idx = indices[dst_vnic]
        key_int = pair_key(src_idx, dst_idx)
        rate_bps = normalize_positive_int(rule.get("rate_bps"), "rate_bps")
        burst_bytes = normalize_positive_int(rule.get("burst_bytes", 262144), "burst_bytes")
        summary = {
            "src_vnic": src_vnic,
            "dst_vnic": dst_vnic,
            "src_vnic_idx": src_idx,
            "dst_vnic_idx": dst_idx,
            "pair_key": key_int,
            "rate_bps": rate_bps,
            "burst_bytes": burst_bytes,
            "generation": generation,
        }
        updates.append(
            MapUpdate(
                "hpft_pair_state",
                pack_u64(key_int),
                pack_pair_state(),
                {**summary, "state": "initial"},
            )
        )
        updates.append(
            MapUpdate(
                "hpft_pair_cfg",
                pack_u64(key_int),
                pack_rate_cfg(rate_bps, generation, burst_bytes),
                summary,
            )
        )
    return updates


def build_commands(
    egress_dev: str,
    pin_dir: Path,
    bpf_object: Path,
    section: str,
    qdisc_mode: str,
    mq_leaf_count: int,
    clear: bool = False,
) -> list[CommandSpec]:
    prog_pin = pin_dir / "hpft_tcp_edt"
    maps_dir = pin_dir / "maps"
    if clear:
        commands = [
            CommandSpec("tc-filter-clear", ["tc", "filter", "delete", "dev", egress_dev, "egress"], True),
            CommandSpec("tc-clsact-clear", ["tc", "qdisc", "delete", "dev", egress_dev, "clsact"], True),
        ]
        if qdisc_mode == "mq-leaf-fq":
            for leaf in range(1, mq_leaf_count + 1):
                commands.append(
                    CommandSpec(
                        "tc-fq-leaf-clear",
                        ["tc", "qdisc", "delete", "dev", egress_dev, "parent", f"0:{tc_handle_id(leaf)}"],
                        True,
                    )
                )
        else:
            commands.append(CommandSpec("tc-root-clear", ["tc", "qdisc", "delete", "dev", egress_dev, "root"], True))
        return commands

    commands: list[CommandSpec] = [
        CommandSpec(
            "bpftool-prog-load",
            [
                bpftool_bin(),
                "prog",
                "load",
                str(bpf_object),
                str(prog_pin),
                "pinmaps",
                str(maps_dir),
            ],
        ),
        CommandSpec("tc-clsact-clear", ["tc", "qdisc", "delete", "dev", egress_dev, "clsact"], True),
        CommandSpec("tc-ingress-clear-for-clsact", ["tc", "qdisc", "delete", "dev", egress_dev, "ingress"], True),
        CommandSpec("tc-clsact", ["tc", "qdisc", "add", "dev", egress_dev, "clsact"]),
        CommandSpec(
            "tc-bpf-attach",
            [
                "tc",
                "filter",
                "replace",
                "dev",
                egress_dev,
                "egress",
                "pref",
                "10",
                "protocol",
                "all",
                "bpf",
                "da",
                "object-pinned",
                str(prog_pin),
            ],
        ),
    ]
    if qdisc_mode == "mq-leaf-fq":
        for leaf in range(1, mq_leaf_count + 1):
            commands.append(
                CommandSpec(
                    "tc-fq-leaf",
                    ["tc", "qdisc", "replace", "dev", egress_dev, "parent", f"0:{tc_handle_id(leaf)}", "fq"],
                )
            )
    else:
        commands.append(CommandSpec("tc-fq-root", ["tc", "qdisc", "replace", "dev", egress_dev, "root", "fq"]))
    return commands


def build_compile_command(source: Path, output: Path) -> CommandSpec:
    return CommandSpec(
        "clang-bpf",
        [
            "clang",
            "-O2",
            "-g",
            "-Wall",
            "-target",
            "bpf",
            # linux/types.h pulls asm/types.h, which lives under the
            # arch-specific include root; a bpf target does not add it
            "-I",
            "/usr/include/x86_64-linux-gnu",
            "-I",
            str(source.parent),
            "-c",
            str(source),
            "-o",
            str(output),
        ],
    )


def build_plan(
    *,
    registry_path: Path,
    registry: dict[str, Any],
    rules_section: str,
    local_host_name: str,
    egress_dev: str,
    pin_dir: Path,
    bpf_object: Path,
    section: str,
    qdisc_mode: str,
    mq_leaf_count: int,
    generation: int,
    resolve_live_ifindex: bool,
    clear: bool = False,
) -> dict[str, Any]:
    rules = select_rules(registry, rules_section)
    updates = [] if clear else build_map_updates(registry, rules, local_host_name, generation, resolve_live_ifindex)
    commands = build_commands(egress_dev, pin_dir, bpf_object, section, qdisc_mode, mq_leaf_count, clear=clear)
    planned_rules = [
        {**update.summary, "egress_dev": egress_dev}
        for update in updates
        if update.map_name == "hpft_pair_cfg"
    ]
    return {
        "backend": "edt-bpf",
        "registry": str(registry_path),
        "rules_section": rules_section,
        "local_host": local_host_name,
        "egress_dev": egress_dev,
        "pin_dir": str(pin_dir),
        "program_pin": str(pin_dir / "hpft_tcp_edt"),
        "map_dir": str(pin_dir / "maps"),
        "bpf_object": str(bpf_object),
        "section": section,
        "qdisc_mode": qdisc_mode,
        "mq_leaf_count": mq_leaf_count,
        "generation": generation,
        "rules": planned_rules,
        "map_updates": [update.as_dict() for update in updates],
        "commands": [command.as_dict() for command in commands],
    }


def run_command(argv: Sequence[str], *, check: bool = True) -> ApplyResult:
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        result = ApplyResult(list(argv), completed.returncode, completed.stdout, completed.stderr)
    except FileNotFoundError as exc:
        result = ApplyResult(list(argv), 127, "", f"{exc}\n")
    if check and result.returncode != 0:
        raise TcpShaperError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr}"
        )
    return result


def bpf_syscall_number() -> int:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return 321
    if machine in ("aarch64", "arm64"):
        return 280
    raise TcpShaperError(f"unsupported architecture for direct bpf syscall: {machine}")


def _bpf_syscall(cmd: int, attr: bytes) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(attr, len(attr))
    ret = libc.syscall(
        ctypes.c_long(bpf_syscall_number()),
        ctypes.c_int(cmd),
        ctypes.c_void_p(ctypes.addressof(buffer)),
        ctypes.c_uint(len(attr)),
    )
    if ret < 0:
        err = ctypes.get_errno()
        raise TcpShaperError(f"bpf cmd {cmd} failed: {os.strerror(err)}")
    return int(ret)


def bpf_obj_get(path: Path) -> int:
    path_buf = ctypes.create_string_buffer(os.fsencode(str(path)) + b"\0")
    attr = struct.pack("<QII", ctypes.addressof(path_buf), 0, 0)
    return _bpf_syscall(BPF_OBJ_GET, attr)


def bpf_map_update_elem(fd: int, key: bytes, value: bytes, flags: int = BPF_ANY) -> None:
    key_buf = ctypes.create_string_buffer(key, len(key))
    value_buf = ctypes.create_string_buffer(value, len(value))
    attr = struct.pack(
        "<I4xQQQ",
        fd,
        ctypes.addressof(key_buf),
        ctypes.addressof(value_buf),
        flags,
    )
    _bpf_syscall(BPF_MAP_UPDATE_ELEM, attr)


class DirectBpfMapWriter:
    """Small direct writer for already pinned BPF maps."""

    def __init__(self, pin_dir: Path) -> None:
        self.pin_dir = pin_dir
        self.paths = map_paths(pin_dir)
        self._fds: dict[str, int] = {}

    def close(self) -> None:
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    def __enter__(self) -> "DirectBpfMapWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def fd(self, map_name: str) -> int:
        if map_name not in self.paths:
            raise TcpShaperError(f"unknown pinned map: {map_name}")
        if map_name not in self._fds:
            path = self.paths[map_name]
            if not path.exists():
                raise TcpShaperError(f"pinned BPF map is missing: {path}")
            self._fds[map_name] = bpf_obj_get(path)
        return self._fds[map_name]

    def update(self, update: MapUpdate) -> int:
        before = time.monotonic_ns()
        bpf_map_update_elem(self.fd(update.map_name), update.key, update.value)
        return time.monotonic_ns() - before


def should_compile(source: Path, output: Path, force: bool) -> bool:
    if force:
        return True
    if not output.exists():
        return True
    return source.stat().st_mtime_ns > output.stat().st_mtime_ns


def apply_plan(
    plan: dict[str, Any],
    map_updates: list[MapUpdate],
    *,
    compile_source: Path,
    compile_output: Path,
    compile_bpf: bool,
    force_compile: bool,
    clear: bool,
) -> list[ApplyResult]:
    results: list[ApplyResult] = []
    pin_dir = Path(str(plan["pin_dir"]))
    maps_dir = Path(str(plan["map_dir"]))
    program_pin = Path(str(plan["program_pin"]))

    if clear:
        for command in plan["commands"]:
            results.append(run_command(command["argv"], check=False))
        return results

    if compile_bpf or should_compile(compile_source, compile_output, force_compile):
        compile_cmd = build_compile_command(compile_source, compile_output)
        results.append(run_command(compile_cmd.argv))

    if not compile_output.exists():
        raise TcpShaperError(f"BPF object not found: {compile_output}")

    pin_dir.mkdir(parents=True, exist_ok=True)
    maps_dir.mkdir(parents=True, exist_ok=True)

    for command in plan["commands"]:
        purpose = str(command["purpose"])
        if purpose == "bpftool-prog-load" and program_pin.exists():
            continue
        results.append(run_command(command["argv"], check=not bool(command.get("ignore_error", False))))

    paths = map_paths(pin_dir)
    missing = [str(path) for path in paths.values() if not path.exists()]
    if missing:
        raise TcpShaperError("pinned BPF maps are missing: " + ", ".join(missing))

    for update in map_updates:
        if update.summary.get("status") == "not_resolved":
            continue
        argv = [
            bpftool_bin(),
            "map",
            "update",
            "pinned",
            str(paths[update.map_name]),
            "key",
            *bpftool_hex_arg(update.key),
            "value",
            *bpftool_hex_arg(update.value),
        ]
        results.append(run_command(argv))
    return results


def make_generation(value: int | None = None) -> int:
    return int(value) if value is not None else time.time_ns()


def build_pair_cfg_update(
    *,
    registry: dict[str, Any],
    src_vnic: str,
    dst_vnic: str,
    rate_bps: int,
    burst_bytes: int,
    generation: int,
    flags: int = 0,
) -> MapUpdate:
    indices = vnic_index(registry)
    if src_vnic not in indices:
        raise TcpShaperError(f"src_vnic not found: {src_vnic}")
    if dst_vnic not in indices:
        raise TcpShaperError(f"dst_vnic not found: {dst_vnic}")

    src_idx = indices[src_vnic]
    dst_idx = indices[dst_vnic]
    key_int = pair_key(src_idx, dst_idx)
    return MapUpdate(
        "hpft_pair_cfg",
        pack_u64(key_int),
        pack_rate_cfg(rate_bps, generation, burst_bytes, flags),
        {
            "src_vnic": src_vnic,
            "dst_vnic": dst_vnic,
            "src_vnic_idx": src_idx,
            "dst_vnic_idx": dst_idx,
            "pair_key": key_int,
            "rate_bps": rate_bps,
            "burst_bytes": burst_bytes,
            "generation": generation,
        },
    )
