"""Check or apply the staged DHCP subnet on two Kea HA nodes from SSH."""
import argparse
import copy
import fcntl
import ipaddress
import json
import os
import time
from datetime import datetime, timezone

from app import EXISTING_VLANS, NODES, PENDING_DIR, configurations, kea, subnets, trunk_preview


def health():
    roles = []
    for node in range(2):
        status = kea(node, "status-get")
        relationships = status.get("high-availability", [])
        if len(relationships) != 1:
            raise ValueError(f"Node {node + 1}: cần đúng một quan hệ HA.")
        relationship = relationships[0]
        servers = relationship["ha-servers"]
        local, remote = servers["local"], servers["remote"]
        if (relationship["ha-mode"] != "hot-standby" or local["state"] != "hot-standby"
                or not remote.get("in-touch") or remote.get("communication-interrupted")):
            raise ValueError(f"Node {node + 1}: HA hot-standby chưa sẵn sàng.")
        roles.append(local["role"])
    if roles != ["standby", "primary"]:
        raise ValueError("KEA_A_URL phải là standby và KEA_B_URL phải là primary.")


def build_candidates(original, plan):
    new_subnet = plan["kea"]["subnet4"]
    interface = plan["kea"]["interface_to_add"]
    network = ipaddress.IPv4Network(new_subnet["subnet"])
    vlan = new_subnet["user-context"]["vlan-id"]
    existing = [subnet for node in original for subnet in subnets(node)]
    if any(network.overlaps(ipaddress.IPv4Network(subnet["subnet"])) for subnet in existing):
        raise ValueError("Subnet mới chồng lấn cấu hình hiện tại.")
    for subnet in existing:
        key = (subnet["id"], subnet["subnet"])
        assigned = (subnet.get("user-context") or {}).get("vlan-id", EXISTING_VLANS.get(key))
        if str(assigned) == str(vlan):
            raise ValueError("VLAN đã có trong Kea.")
    new_subnet["id"] = max((subnet["id"] for subnet in existing), default=0) + 1
    candidates = copy.deepcopy(original)
    for candidate in candidates:
        if (candidate.get("config-control") or {}).get("config-databases"):
            raise ValueError("Chỉ hỗ trợ cấu hình Kea lưu bằng file.")
        interfaces = candidate["interfaces-config"]["interfaces"]
        if "*" not in interfaces and interface not in interfaces:
            interfaces.append(interface)
        candidate.setdefault("subnet4", []).append(new_subnet)
    return candidates, new_subnet


def verify_subnet(expected):
    for node, config in enumerate(configurations(), 1):
        match = next((subnet for subnet in subnets(config) if subnet["id"] == expected["id"]), {})
        if any(match.get(key) != expected[key] for key in ("subnet", "interface", "user-context")):
            raise ValueError(f"Node {node}: không tìm thấy subnet mới sau khi nạp.")
        normalize = lambda pools: {pool["pool"].replace(" ", "") for pool in pools}
        if normalize(match.get("pools", [])) != normalize(expected["pools"]):
            raise ValueError(f"Node {node}: pool sau khi nạp không khớp.")


def wait_for_ha():
    deadline = time.monotonic() + 45
    while True:
        try:
            health()
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def run(apply, prepare=False):
    if not all(NODES) or NODES[0] == NODES[1]:
        raise ValueError("Cần hai URL Kea khác nhau.")
    PENDING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (PENDING_DIR / "apply.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = PENDING_DIR / "pending-vlan.json"
        raw = pending.read_bytes()
        plan = trunk_preview(json.loads(raw))
        health()
        original = configurations()
        candidates, expected = build_candidates(original, plan)
        for node, candidate in enumerate(candidates):
            commands = kea(node, "list-commands")
            if not {"config-test", "config-set", "config-write"}.issubset(commands):
                raise ValueError(f"Node {node + 1}: thiếu lệnh kiểm tra, nạp hoặc lưu cấu hình.")
            kea(node, "config-test", {"Dhcp4": candidate})
        print(f"Hai node đã qua config-test. Subnet {expected['subnet']}, ID {expected['id']}.", flush=True)
        if not apply and not prepare:
            return
        if configurations() != original or pending.read_bytes() != raw:
            raise ValueError("Cấu hình hoặc file chờ vừa thay đổi. Hãy kiểm tra lại.")
        backup = PENDING_DIR / datetime.now(timezone.utc).strftime("backup-%Y%m%dT%H%M%S-%fZ")
        backup.mkdir(mode=0o700)
        for node, config in enumerate(original, 1):
            (backup / f"node-{node}.json").write_text(json.dumps({"Dhcp4": config}, indent=2), encoding="utf-8")
        print(f"Bản sao cấu hình: {backup}", flush=True)
        if prepare:
            for node, config in enumerate(candidates, 1):
                path = backup / f"node-{node}.next.json"
                path.write_text(json.dumps({"Dhcp4": config}, indent=2) + "\n", encoding="utf-8")
                print(f"Node {node}: {path}", flush=True)
            return
        attempted = []
        try:
            for node, candidate in enumerate(candidates):
                attempted.append(node)
                kea(node, "config-set", {"Dhcp4": candidate})
                kea(node, "config-write")
            verify_subnet(expected)
            wait_for_ha()
        except (Exception, KeyboardInterrupt):
            for node in reversed(attempted):
                try:
                    kea(node, "config-set", {"Dhcp4": original[node]})
                    kea(node, "config-write")
                    print(f"Node {node + 1}: đã nạp lại và lưu cấu hình cũ.", flush=True)
                except Exception as error:
                    print(f"Node {node + 1}: hoàn tác lỗi, cần đối soát từ bản sao: {error}", flush=True)
            raise
        print("Đã áp dụng và xác minh subnet mới trên cả hai node.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Ghi vào hai node sau khi kiểm tra")
    mode.add_argument("--prepare", action="store_true", help="Xuất cấu hình đã kiểm tra, không nạp Kea")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args.apply, args.prepare)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit(f"Dừng: {error}") from error
