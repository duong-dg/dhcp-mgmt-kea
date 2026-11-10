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

Mật khẩu web là bắt buộc. Dashboard giới hạn 5 lần đăng nhập sai mỗi
phút theo địa chỉ kết nối. Với reverse proxy, giới hạn này được chia sẻ
cho các kết nối đến từ cùng proxy. Phiên đăng nhập hết hạn sau 8 giờ
hoặc khi dịch vụ khởi động lại. Nếu dùng HTTPS qua reverse proxy, đặt
KEA_WEB_ORIGIN đúng với URL HTTPS để cookie chỉ được gửi qua HTTPS.

## HTTPS trực tiếp

Cài chứng thư có SAN khớp địa chỉ IP máy chủ và được máy khách tin cậy.
Đặt KEA_TLS_CERT và KEA_TLS_KEY thành đường dẫn PEM trên máy chủ, rồi
khởi động lại dịch vụ và mở https://<IP>:<PORT>/. Tài khoản chạy web
cần quyền đọc khóa riêng. Nếu dùng HTTPS trực tiếp, bỏ KEA_WEB_ORIGIN
dành cho reverse proxy.

Bấm tên VLAN để xem sức chứa pool, lease hoạt động và các dải ngoài
pool động. Số chưa có lease hoạt động được tính từ pool trừ lease đang
hoạt động trong pool. IP giữ chỗ hoặc bị từ chối vẫn có thể không khả
dụng. Các dải ngoài pool động vẫn có thể chứa IP tĩnh hoặc reservation.

## Thử gửi OTP qua SMTP

Đặt các biến dưới đây trong EnvironmentFile của systemd, giữ quyền 600.
Web cần chạy HTTPS. Người nhận cố định từ cấu hình máy chủ.

```ini
KEA_SMTP_HOST=smtp.example.net
KEA_SMTP_PORT=587
KEA_SMTP_TLS=starttls
KEA_SMTP_USER=sender@example.net
KEA_SMTP_PASSWORD="MAT_KHAU_SMTP"
KEA_SMTP_FROM=sender@example.net
KEA_SMTP_RECIPIENT=admin@example.net
```

Nếu nhà cung cấp dùng TLS trực tiếp, đặt KEA_SMTP_TLS=ssl và cổng phù hợp.
Khởi động lại dịch vụ, xem trước một VLAN, bấm Gửi OTP thử rồi nhập mã.
OTP thử không ghi Netplan hoặc Kea. Cấu hình SMTP hiện nhập trên máy chủ.

## Lưu kế hoạch và áp dụng qua SSH

Sau khi xác minh OTP và nhập lại mật khẩu, web lưu một kế hoạch chờ
trong ~/.kea-dashboard/pending-vlan.json của tài khoản chạy dịch vụ.
Kế hoạch mới thay thế kế hoạch chờ trước đó.

Trình apply_vlan.py chạy trên Linux. Nó chỉ cấu hình Kea cho interface
VLAN đã tồn tại và có IP đúng trên cả hai máy. KEA_A_URL phải trỏ tới
standby, KEA_B_URL phải trỏ tới primary. Chạy bằng cùng tài khoản và
EnvironmentFile như dịch vụ web. Ví dụ:

```bash
sudo systemd-run --wait --pipe --collect --uid=WEB_USER_LINUX \
  -p EnvironmentFile=/etc/kea-dashboard.env \
  /usr/bin/python3 /DUONG_DAN_REPO/apply_vlan.py
```

Lệnh mặc định chỉ kiểm tra. Thêm --apply để sao lưu, nạp và lưu Kea trên
hai node. Tránh chạy cùng lúc với công cụ khác đang quản lý cấu hình.
Nếu thao tác lỗi sau khi bắt đầu ghi, script thử nạp và lưu lại cấu hình
cũ. Nếu hoàn tác lỗi, dùng bản sao được báo trên terminal để đối soát.
Giữ các bản sao trên máy chủ vì chúng chứa cấu hình Kea đầy đủ.
