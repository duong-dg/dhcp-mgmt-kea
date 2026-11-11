"""Check or apply the staged DHCP subnet on two Kea HA nodes from SSH."""
import argparse
import copy
import fcntl
import ipaddress
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app import EXISTING_VLANS, NODES, PENDING_DIR, configurations, kea, subnets, plan_preview


def health():
    roles = []
    with ThreadPoolExecutor(2) as executor:
        statuses = list(executor.map(lambda node: kea(node, "status-get"), range(2)))
    for node, status in enumerate(statuses):
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
        if expected.get("kind") in ("reservation", "remove-reservation"):
            present = any(
                all(item.get(key) == value for key, value in expected["reservation"].items())
                for item in match.get("reservations", [])
            )
            if match.get("subnet") != expected["subnet"] or present != (expected["kind"] == "reservation"):
                raise ValueError(f"Node {node}: reservation sau khi nạp không khớp.")
            continue
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
            time.sleep(1)


def reservation_candidates(original, plan):
    candidates = copy.deepcopy(original)
    wanted = plan["reservation"]
    address, mac = wanted["ip-address"], wanted["hw-address"]
    for node, config in enumerate(candidates):
        subnet = next((item for item in config.get("subnet4", [])
                       if item["id"] == plan["id"] and item["subnet"] == plan["subnet"]), None)
        if subnet is None:
            raise ValueError(f"Node {node + 1}: cần subnet độc lập khớp với lease.")
        if config.get("hosts-database") or config.get("hosts-databases") or (
                config.get("config-control") or {}).get("config-databases"):
            raise ValueError(f"Node {node + 1}: chỉ hỗ trợ reservation lưu bằng file.")
        reservations = subnet.setdefault("reservations", [])
        if plan["kind"] == "remove-reservation":
            matches = [item for item in reservations if item.get("ip-address") == address
                       and item.get("hw-address", "").lower() == mac]
            if len(matches) != 1:
                raise ValueError(f"Node {node + 1}: không tìm thấy đúng một reservation IP/MAC để bỏ.")
            reservations.remove(matches[0])
            continue
        if (not subnet.get("reservations-in-subnet", config.get("reservations-in-subnet", True))
                or subnet.get("reservations-out-of-pool", config.get("reservations-out-of-pool", False))
                or "hw-address" not in config.get("host-reservation-identifiers", ["hw-address"])):
            raise ValueError(f"Node {node + 1}: chế độ reservation hiện tại không hỗ trợ thao tác này.")
        leases = kea(node, "lease4-get", {"ip-address": address}, allow_empty=True)
        if (leases.get("hw-address", "").lower() != mac or leases.get("subnet-id") != plan["id"]
                or leases.get("state", 0) != 0
                or leases.get("cltt", 0) + leases.get("valid-lft", 0) <= time.time()):
            raise ValueError(f"Node {node + 1}: lease không còn hoạt động hoặc MAC đã thay đổi.")
        if any(item.get("ip-address") == address or item.get("hw-address", "").lower() == mac
               for item in [*config.get("reservations", []), *reservations]):
            raise ValueError(f"Node {node + 1}: IP hoặc MAC đã có reservation.")
        reservations.append(wanted)
    return candidates, plan


def run(apply, prepare=False, reservation=None, draft=False):
    if draft and (apply or reservation is not None):
        raise ValueError("Chế độ xuất bản nháp không được dùng để áp dụng.")
    if not all(NODES) or NODES[0] == NODES[1]:
        raise ValueError("Cần hai URL Kea khác nhau.")
    PENDING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (PENDING_DIR / "apply.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = PENDING_DIR / "pending-vlan.json"
        raw = pending.read_bytes() if reservation is None else None
        plan = plan_preview(json.loads(raw)) if reservation is None else reservation
        health()
        original = configurations()
        if plan.get("kind") in ("reservation", "remove-reservation"):
            candidates, expected = reservation_candidates(original, plan)
        else:
            candidates, expected = build_candidates(original, plan)
        def test_candidate(node):
            commands = kea(node, "list-commands")
            if not {"config-test", "config-set", "config-write"}.issubset(commands):
                raise ValueError(f"Node {node + 1}: thiếu lệnh kiểm tra, nạp hoặc lưu cấu hình.")
            kea(node, "config-test", {"Dhcp4": candidates[node]})

        if draft:
            print("Bản nháp: chưa qua config-test. Cần kiểm tra file bằng Kea trên từng node trước khi nạp.", flush=True)
        else:
            with ThreadPoolExecutor(2) as executor:
                list(executor.map(test_candidate, range(2)))
            print(f"Hai node đã qua config-test. Subnet {expected['subnet']}, ID {expected['id']}.", flush=True)
        if not apply and not prepare and not draft:
            return
        if configurations() != original or (raw is not None and pending.read_bytes() != raw):
            raise ValueError("Cấu hình hoặc file chờ vừa thay đổi. Hãy kiểm tra lại.")
        backup = PENDING_DIR / datetime.now(timezone.utc).strftime("backup-%Y%m%dT%H%M%S-%fZ")
        backup.mkdir(mode=0o700)
        for node, config in enumerate(original, 1):
            (backup / f"node-{node}.json").write_text(json.dumps({"Dhcp4": config}, indent=2), encoding="utf-8")
        print(f"Bản sao cấu hình: {backup}", flush=True)
        if prepare or draft:
            for node, config in enumerate(candidates, 1):
                path = backup / f"node-{node}.next.json"
                path.write_text(json.dumps({"Dhcp4": config}, indent=2) + "\n", encoding="utf-8")
                print(f"Node {node}: {path}", flush=True)
            (backup / "validation.json").write_text(
                json.dumps({"api_config_test_passed": not draft}, indent=2) + "\n", encoding="utf-8")
            return
        if reservation is not None:
            # Verify persistence of each node's current configuration before changing either node.
            for node in range(2):
                kea(node, "config-write")
            if configurations() != original:
                raise ValueError("Cấu hình Kea vừa thay đổi. Hãy làm mới và thử lại.")
        attempted = []
        try:
            for node, candidate in enumerate(candidates):
                attempted.append(node)
                kea(node, "config-set", {"Dhcp4": candidate})
                kea(node, "config-write")
            verify_subnet(expected)
            wait_for_ha()
        except (Exception, KeyboardInterrupt) as error:
            failures = []
            for node in reversed(attempted):
                try:
                    kea(node, "config-set", {"Dhcp4": original[node]})
                    kea(node, "config-write")
                    print(f"Node {node + 1}: đã nạp lại và lưu cấu hình cũ.", flush=True)
                except Exception as rollback_error:
                    failures.append(f"Node {node + 1}: {rollback_error}")
            if failures:
                raise ValueError(f"{error} Hoàn tác chưa hoàn tất. Bản sao: {backup}. "
                                 + " | ".join(failures)) from error
            raise
        print("Đã áp dụng và xác minh subnet mới trên cả hai node.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Ghi vào hai node sau khi kiểm tra")
    mode.add_argument("--prepare", action="store_true", help="Xuất cấu hình đã kiểm tra, không nạp Kea")
    mode.add_argument("--draft", action="store_true", help="Chỉ xuất bản nháp, cần kiểm tra cú pháp trên từng node")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run(args.apply, args.prepare, draft=args.draft)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit(f"Dừng: {error}") from error
