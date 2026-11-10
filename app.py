import ipaddress as ip
import base64
import hmac
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

NODES = [os.getenv("KEA_A_URL"), os.getenv("KEA_B_URL")]
WRITES = os.getenv("KEA_ENABLE_WRITES") == "1"
WEB_BIND = os.getenv("KEA_WEB_BIND", "127.0.0.1")
WEB_USER = os.getenv("KEA_WEB_USER", "admin")
WEB_PASSWORD = os.getenv("KEA_WEB_PASSWORD", "")
WEB_ORIGIN = os.getenv("KEA_WEB_ORIGIN")
TRUNK = os.getenv("KEA_TRUNK_INTERFACE", "eth0")
EXISTING_VLANS = {
    (int(item["subnet_id"]), item["cidr"]): int(item["vlan"])
    for item in json.loads(os.getenv("KEA_VLAN_MAP", "[]"))
}


def kea(node, command, arguments=None, allow_empty=False):
    url = NODES[node]
    if not url:
        raise ValueError("Cần cấu hình KEA_A_URL và KEA_B_URL.")
    message = {"command": command}
    if os.getenv("KEA_DIRECT") != "1":
        message["service"] = ["dhcp4"]
    if arguments is not None:
        message["arguments"] = arguments
    headers = {"Content-Type": "application/json"}
    auth = os.getenv(("KEA_A_AUTH", "KEA_B_AUTH")[node])
    if auth:
        headers["Authorization"] = auth
    try:
        with urlopen(Request(url, json.dumps(message).encode(), headers), timeout=8) as response:
            reply = json.load(response)
        if isinstance(reply, list):
            reply, = reply
        if reply["result"] == 0 or (allow_empty and reply["result"] == 3):
            return reply.get("arguments", {})
        raise ValueError(reply.get("text", "Kea từ chối lệnh"))
    except Exception as error:
        raise ValueError(f"Node {node + 1} / {command}: {error}") from error


def subnets(config):
    yield from config.get("subnet4", [])
    for network in config.get("shared-networks", []):
        yield from network.get("subnet4", [])


def configurations():
    return [kea(node, "config-get")["Dhcp4"] for node in range(2)]


def dashboard():
    with ThreadPoolExecutor(2) as executor:
        nodes = list(executor.map(read_node, range(2)))
    rows = {}
    leases = {}
    now = time.time()
    for node, (config, entries, errors) in enumerate(nodes, 1):
        for subnet in subnets(config):
            key = (subnet["id"], subnet["subnet"])
            row = rows.setdefault(key, {
                "id": key[0], "cidr": key[1],
                "vlan": (subnet.get("user-context") or {}).get("vlan-id", EXISTING_VLANS.get(key)),
                "pools": [pool["pool"] for pool in subnet.get("pools", [])],
                "nodes": [],
            })
            row["nodes"].append(node)
        for lease in entries:
            if lease.get("state", 0) == 0 and lease["cltt"] + lease["valid-lft"] > now:
                key = (lease["subnet-id"], lease["ip-address"])
                if key not in leases or lease["cltt"] > leases[key]["cltt"]:
                    leases[key] = lease
    for row in rows.values():
        row["leases"] = [
            lease for (subnet_id, address), lease in leases.items()
            if subnet_id == row["id"] and ip.IPv4Address(address) in ip.IPv4Network(row["cidr"])
        ]
        row["leases"].sort(key=lambda lease: ip.IPv4Address(lease["ip-address"]))
    return {"rows": list(rows.values()), "errors": [e for _, _, errors in nodes for e in errors],
            "writes": WRITES, "total": len(leases)}


def read_node(node):
    errors = []
    config, leases = {}, []
    try:
        config = kea(node, "config-get")["Dhcp4"]
    except ValueError as error:
        errors.append(str(error))
    try:
        leases = kea(node, "lease4-get-all", allow_empty=True).get("leases", [])
    except ValueError as error:
        errors.append(str(error))
    return config, leases, errors


def vlan_plan(data):
    vlan = int(data["vlan"])
    network = ip.IPv4Network(data["cidr"])
    start, end, gateway = (ip.IPv4Address(data[key]) for key in ("start", "end", "gateway"))
    if not 1 <= vlan <= 4094:
        raise ValueError("VLAN ID phải từ 1 đến 4094.")
    if not (network.network_address < start <= end < network.broadcast_address
            and network.network_address < gateway < network.broadcast_address
            and not start <= gateway <= end):
        raise ValueError("Pool và gateway phải nằm trong subnet. Gateway phải nằm ngoài pool.")
    options = [{"name": "routers", "data": str(gateway)}]
    dns = [str(ip.IPv4Address(value.strip())) for value in data.get("dns", "").split(",") if value.strip()]
    if dns:
        options.append({"name": "domain-name-servers", "data": ", ".join(dns)})
    return vlan, network, start, end, gateway, options


def trunk_preview(data):
    vlan, network, start, end, gateway, options = vlan_plan(data)
    addresses = [ip.IPv4Address(data[key]) for key in ("standby", "primary")]
    if len(set(addresses)) != 2 or any(
        not network.network_address < address < network.broadcast_address
        or address == gateway or start <= address <= end for address in addresses
    ):
        raise ValueError("IP hai node phải khác nhau, nằm trong subnet và nằm ngoài pool, gateway.")
    interface = f"{TRUNK}.{vlan}"
    return {
        "netplan": {
            node: {"network": {"version": 2, "vlans": {interface: {
                "id": vlan, "link": TRUNK, "addresses": [f"{address}/{network.prefixlen}"],
            }}}}
            for node, address in zip(("standby", "primary"), addresses)
        },
        "kea": {"interface_to_add": interface, "subnet4": {
            "subnet": str(network), "interface": interface,
            "user-context": {"vlan-id": vlan},
            "pools": [{"pool": f"{start} - {end}"}], "option-data": options,
        }},
    }


def create(data):
    if not WRITES:
        raise ValueError("Chế độ chỉ đọc.")
    vlan, network, start, end, _, options = vlan_plan(data)
    config = configurations()
    existing = [subnet for item in config for subnet in subnets(item)]
    if any(network.overlaps(ip.IPv4Network(s["subnet"])) for s in existing):
        raise ValueError("Subnet chồng lấn với cấu hình trên ít nhất một node.")
    if any(str((s.get("user-context") or {}).get(
            "vlan-id", EXISTING_VLANS.get((s["id"], s["subnet"])))) == str(vlan) for s in existing):
        raise ValueError("VLAN đã được khai báo.")
    subnet_id = max((s["id"] for s in existing), default=0) + 1
    subnet = {"id": subnet_id, "subnet": str(network), "user-context": {"vlan-id": vlan},
              "pools": [{"pool": f"{start} - {end}"}], "option-data": options}
    for node, current in enumerate(config):
        commands = kea(node, "list-commands")
        # list-commands returns the command names directly in arguments.
        if not all(cmd in commands for cmd in ("subnet4-add", "subnet4-del", "config-write")):
            raise ValueError(f"Node {node + 1} thiếu lệnh quản lý subnet hoặc lưu cấu hình.")
        if current.get("config-control", {}).get("config-databases"):
            raise ValueError("Chế độ ghi chỉ hỗ trợ cấu hình Kea lưu bằng file.")
        candidate = {**current, "subnet4": [*current.get("subnet4", []), subnet]}
        kea(node, "config-test", {"Dhcp4": candidate})
    if configurations() != config:
        raise ValueError("Cấu hình vừa thay đổi. Hãy thử lại.")
    attempted = []
    try:
        for node in range(2):
            attempted.append(node)
            kea(node, "subnet4-add", {"subnet4": [subnet]})
        for node in range(2):
            kea(node, "config-write")
    except ValueError as error:
        failures = []
        for node in reversed(attempted):
            try:
                current = kea(node, "config-get")["Dhcp4"]
                match = next((s for s in subnets(current) if s["id"] == subnet_id), None)
                if match is not None:
                    if match["subnet"] != str(network) or (match.get("user-context") or {}).get("vlan-id") != vlan:
                        raise ValueError("Subnet ID đã được dùng bởi thay đổi khác.")
                    kea(node, "subnet4-del", {"id": subnet_id})
                kea(node, "config-write")
            except ValueError as rollback_error:
                failures.append(str(rollback_error))
        suffix = " Cần đối soát cấu hình: " + " | ".join(failures) if failures else " Đã hoàn tác."
        raise ValueError(str(error) + suffix) from error
    return {"id": subnet_id}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content, kind="application/json"):
        payload = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.reply(403, {"error": "Host không hợp lệ."})
            return False
        if not WEB_PASSWORD:
            return True
        token = base64.b64encode(f"{WEB_USER}:{WEB_PASSWORD}".encode()).decode()
        actual = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(actual, f"Basic {token}".encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Kea DHCP Manager", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/":
            self.reply(200, Path(__file__).with_name("index.html").read_bytes(), "text/html")
        elif self.path == "/api/dashboard":
            self.reply(200, dashboard())
        else:
            self.reply(404, {"error": "Không tìm thấy."})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path not in ("/api/subnets", "/api/vlan-plan"):
            return self.reply(404, {"error": "Không tìm thấy."})
        origin = WEB_ORIGIN or f"http://{self.headers.get('Host')}"
        if self.headers.get("Origin") != origin or self.headers.get_content_type() != "application/json":
            return self.reply(403, {"error": "Yêu cầu không hợp lệ."})
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192:
                raise ValueError("Kích thước dữ liệu không hợp lệ.")
            data = json.loads(self.rfile.read(size))
            if self.path == "/api/vlan-plan":
                self.reply(200, trunk_preview(data))
            else:
                self.reply(201, create(data))
        except (ValueError, KeyError, TypeError) as error:
            self.reply(400, {"error": str(error)})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    if WEB_BIND != "127.0.0.1":
        raise SystemExit("Dùng reverse proxy HTTPS trước backend loopback.")
    if WEB_ORIGIN and (not WEB_ORIGIN.startswith("https://") or not WEB_PASSWORD):
        raise SystemExit("Truy cập qua proxy yêu cầu HTTPS và KEA_WEB_PASSWORD.")
    print(f"Kea DHCP Manager: http://127.0.0.1:{port}", flush=True)
    HTTPServer((WEB_BIND, port), Handler).serve_forever()
