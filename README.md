# Kea DHCP Manager

Dashboard nội bộ cho Kea DHCPv4 HA. Web hiển thị subnet, pool, số lease còn hiệu lực và chi tiết IP, MAC, hostname, thời hạn lease. Lease trùng giữa hai node được gộp theo subnet ID và IP.

## Chạy

Cần Python 3.10+. Trên Windows, chạy `.Start-KeaDashboard.ps1` để nhập URL và tài khoản API của hai node. Trên Linux, chạy `python3 app.py` sau khi thiết lập các biến môi trường sau.

| Biến | Giá trị |
|---|---|
| KEA_A_URL, KEA_B_URL | URL của hai endpoint quản trị |
| KEA_A_AUTH, KEA_B_AUTH | Giá trị Authorization của từng endpoint nếu có |
| KEA_DIRECT | Đặt 1 nếu kết nối HTTP trực tiếp tới DHCPv4. Mặc định dùng Control Agent |
| KEA_TRUNK_INTERFACE | Tên card trunk để sinh cấu hình xem trước |
| KEA_VLAN_MAP | JSON tùy chọn, ví dụ [{"subnet_id":20,"cidr":"192.0.2.0/24","vlan":20}]. Metadata Kea được ưu tiên |
| KEA_WEB_USER, KEA_WEB_PASSWORD | Tài khoản riêng của web |
| KEA_WEB_ORIGIN | Origin HTTPS bên ngoài nếu dùng reverse proxy |

Mở http://127.0.0.1:8080. Khi chạy trên máy chủ, dùng SSH port forwarding hoặc reverse proxy HTTPS. Với reverse proxy, chuyển tiếp Authorization và đặt upstream Host thành 127.0.0.1:8080. Lưu bí mật bên ngoài repository. Hai node cần hỗ trợ config-get và lease4-get-all.

Form VLAN tạo bản xem trước Netplan và Kea. Việc áp dụng cấu hình mạng lên hai node thực hiện riêng theo quy trình quản trị của hệ thống.

Để chạy thử trực tiếp trên mạng quản trị, đặt KEA_WEB_BIND thành IPv4 của
máy chủ, PORT thành cổng mong muốn và KEA_WEB_PASSWORD thành mật khẩu web.
Chế độ này dùng HTTP. Dùng HTTPS qua reverse proxy hoặc SSH tunnel nếu
cần mã hóa thông tin đăng nhập.
