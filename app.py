import ipaddress as ip
import base64
import hmac
import json
import os
import secrets
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

NODES = [os.getenv("KEA_A_URL"), os.getenv("KEA_B_URL")]
WRITES = os.getenv("KEA_ENABLE_WRITES") == "1"
WEB_BIND = os.getenv("KEA_WEB_BIND", "127.0.0.1")
WEB_USER = os.getenv("KEA_WEB_USER", "admin")
WEB_PASSWORD = os.getenv("KEA_WEB_PASSWORD", "")
WEB_ORIGIN = os.getenv("KEA_WEB_ORIGIN")
TLS_CERT = os.getenv("KEA_TLS_CERT")
TLS_KEY = os.getenv("KEA_TLS_KEY")
SECURE_WEB = bool(TLS_CERT or WEB_ORIGIN)
SESSION_KEY = secrets.token_bytes(32)
FAILED_LOGINS = {}
LOGIN_WINDOW = 60
LOGIN_LIMIT = 5
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


def pool_summary(row):
    network = ip.IPv4Network(row["cidr"])
    first = int(network.network_address) + (network.prefixlen < 31)
    last = int(network.broadcast_address) - (network.prefixlen < 31)
    ranges = []
    for pool in row["pools"]:
        if "-" in pool:
            start, end = (ip.IPv4Address(value.strip()) for value in pool.split("-", 1))
        else:
            prefix = ip.IPv4Network(pool.strip())
            start, end = prefix.network_address, prefix.broadcast_address
        ranges.append((int(start), int(end)))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    excluded = []
    cursor = first
    for start, end in [*merged, (last + 1, last + 1)]:
        if cursor < start:
            excluded.append([str(ip.IPv4Address(cursor)), str(ip.IPv4Address(start - 1))])
        cursor = max(cursor, end + 1)
    used = sum(any(start <= int(ip.IPv4Address(lease["ip-address"])) <= end
                   for start, end in merged) for lease in row["leases"])
    size = sum(end - start + 1 for start, end in merged)
    return {"size": size, "used": used, "remaining": size - used, "excluded": excluded}


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
        row["capacity"] = pool_summary(row)
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


def vlan_plan(data, allow_gateway_in_range=False):
    vlan = int(data["vlan"])
    network = ip.IPv4Network(data["cidr"])
    start, end, gateway = (ip.IPv4Address(data[key]) for key in ("start", "end", "gateway"))
    if not 1 <= vlan <= 4094:
        raise ValueError("VLAN ID phải từ 1 đến 4094.")
    if not (network.network_address < start <= end < network.broadcast_address
            and network.network_address < gateway < network.broadcast_address):
        raise ValueError("Dải cấp DHCP và gateway phải nằm trong phần IP dùng được của subnet.")
    if not allow_gateway_in_range and start <= gateway <= end:
        raise ValueError("Gateway phải nằm ngoài pool.")
    options = [{"name": "routers", "data": str(gateway)}]
    dns = [str(ip.IPv4Address(value.strip())) for value in data.get("dns", "").split(",") if value.strip()]
    if dns:
        options.append({"name": "domain-name-servers", "data": ", ".join(dns)})
    return vlan, network, start, end, gateway, options


def trunk_preview(data):
    vlan, network, start, end, gateway, options = vlan_plan(data, allow_gateway_in_range=True)
    addresses = [ip.IPv4Address(data[key]) for key in ("standby", "primary")]
    if len(set(addresses)) != 2 or any(
        not network.network_address < address < network.broadcast_address
        or address == gateway for address in addresses
    ):
        raise ValueError("IP hai node phải nằm trong subnet, khác nhau và khác gateway.")
    exclusions = [(int(address), int(address)) for address in [gateway, *addresses]]
    for item in data.get("exclude", "").replace(",", "\n").splitlines():
        if not item.strip():
            continue
        bounds = item.split("-", 1)
        first = ip.IPv4Address(bounds[0].strip())
        last = ip.IPv4Address(bounds[-1].strip())
        if not start <= first <= last <= end:
            raise ValueError(f"Dải loại trừ phải nằm trong dải dự kiến cấp DHCP: {item.strip()}")
        exclusions.append((int(first), int(last)))
    pools = []
    cursor = int(start)
    limit = int(end)
    for first, last in sorted(exclusions):
        if last < cursor:
            continue
        if first > limit:
            break
        if cursor < first:
            pools.append({"pool": f"{ip.IPv4Address(cursor)} - {ip.IPv4Address(first - 1)}"})
        cursor = last + 1
    if cursor <= limit:
        pools.append({"pool": f"{ip.IPv4Address(cursor)} - {end}"})
    if not pools:
        raise ValueError("Không còn IP để cấp DHCP sau khi áp dụng loại trừ.")
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
            "pools": pools, "option-data": options,
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
    def web_origin(self):
        scheme = "https" if TLS_CERT else "http"
        return WEB_ORIGIN or f"{scheme}://{self.headers.get('Host')}"

    def reply(self, status, content, kind="application/json", headers=()):
        payload = content if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        if self.headers.get("Host") != f"{WEB_BIND}:{self.server.server_port}":
            self.reply(403, {"error": "Host không hợp lệ."})
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie["kea_session"].value
            expiry = int(token.split(".", 1)[0])
            if expiry > time.time() and hmac.compare_digest(token.encode(), self.session_token(expiry).encode()):
                return True
        except (CookieError, KeyError, ValueError):
            pass
        if self.path.startswith("/api/"):
            self.reply(401, {"error": "Phiên đăng nhập đã hết hạn."})
        else:
            self.reply(303, b"", headers=[("Location", "/login")])
        return False

    def session_token(self, expiry):
        digest = hmac.digest(SESSION_KEY, f"{WEB_USER}:{expiry}".encode(), "sha256")
        return f"{expiry}.{base64.urlsafe_b64encode(digest).decode()}"

    def login_page(self, failed=False):
        page = Path(__file__).with_name("login.html").read_text(encoding="utf-8")
        error = "Tên đăng nhập hoặc mật khẩu không đúng." if failed else ""
        self.reply(401 if failed else 200, page.replace("{{error}}", error).encode(), "text/html")

    def login_post(self):
        host = self.headers.get("Host")
        if host != f"{WEB_BIND}:{self.server.server_port}" or self.headers.get("Origin") != self.web_origin():
            return self.reply(403, {"error": "Yêu cầu không hợp lệ."})
        secure = "; Secure" if SECURE_WEB else ""
        if self.path == "/logout":
            return self.reply(303, b"", headers=[
                ("Location", "/login"),
                ("Set-Cookie", f"kea_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"),
            ])
        now = time.monotonic()
        for address, (_, deadline) in list(FAILED_LOGINS.items()):
            if deadline <= now:
                del FAILED_LOGINS[address]
        address = self.client_address[0]
        failures, deadline = FAILED_LOGINS.get(address, (0, now + LOGIN_WINDOW))
        if failures >= LOGIN_LIMIT:
            return self.reply(429, {"error": "Đăng nhập sai quá nhiều lần. Vui lòng thử lại sau."},
                              headers=[("Retry-After", str(max(1, int(deadline - now))))])
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 8192 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError
            fields = parse_qs(self.rfile.read(size).decode())
        except ValueError:
            return self.reply(400, {"error": "Yêu cầu không hợp lệ."})
        user_ok = hmac.compare_digest(fields.get("username", [""])[0].encode(), WEB_USER.encode())
        password_ok = hmac.compare_digest(fields.get("password", [""])[0].encode(), WEB_PASSWORD.encode())
        if not (user_ok and password_ok):
            FAILED_LOGINS[address] = (failures + 1, deadline)
            return self.login_page(failed=True)
        FAILED_LOGINS.pop(address, None)
        age = 8 * 3600
        token = self.session_token(int(time.time()) + age)
        self.reply(303, b"", headers=[
            ("Location", "/"),
            ("Set-Cookie", f"kea_session={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict{secure}"),
        ])

    def do_GET(self):
        if self.path == "/login" and self.headers.get("Host") == f"{WEB_BIND}:{self.server.server_port}":
            return self.login_page()
        if not self.authorized():
            return
        if urlsplit(self.path).path in ("/", "/vlan"):
            self.reply(200, Path(__file__).with_name("index.html").read_bytes(), "text/html")
        elif self.path == "/api/dashboard":
            self.reply(200, dashboard())
        else:
            self.reply(404, {"error": "Không tìm thấy."})

    def do_POST(self):
        if self.path in ("/login", "/logout"):
            return self.login_post()
        if not self.authorized():
            return
        if self.path not in ("/api/subnets", "/api/vlan-plan"):
            return self.reply(404, {"error": "Không tìm thấy."})
        origin = self.web_origin()
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
    if not WEB_PASSWORD.strip():
        raise SystemExit("Cần đặt KEA_WEB_PASSWORD để khởi động dashboard.")
    if WEB_ORIGIN and not WEB_ORIGIN.startswith("https://"):
        raise SystemExit("Truy cập qua proxy yêu cầu HTTPS.")
    if bool(TLS_CERT) != bool(TLS_KEY):
        raise SystemExit("HTTPS yêu cầu cả KEA_TLS_CERT và KEA_TLS_KEY.")
    with HTTPServer((WEB_BIND, port), Handler) as server:
        if TLS_CERT:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(TLS_CERT, TLS_KEY)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https" if TLS_CERT else "http"
        print(f"Kea DHCP Manager: {scheme}://{WEB_BIND}:{port}", flush=True)
        server.serve_forever()
