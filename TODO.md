# STATE HANDOFF (Zer0mem)
1. **Đã hoàn thành**: Kết nối thành công Proxy Dân cư Viettel HCM (`222.255.182.252:29907` -> IP đầu ra `171.246.111.124`) vào `config/secrets.yaml` cho tài khoản `DEA209907000` qua tên miền chính thức `https://www.bong88.com/b/vn` (`200 OK`).
2. **Trạng thái hiện tại**: Đã tách biệt hoàn toàn luồng `self._scanner_http` (Quét bảng kèo `wonderfulnextapi.com` tốc độ cao `378ms`, tốn `0 MB` băng thông Proxy) và luồng `self._http` (Gắn cố định Proxy HCM `171.246.111.124` chuyên dùng cho `bong88.com` & đặt cược).
3. **Task tiếp theo**: Bấm `Ctrl + C` và chạy lại `python src/main.py` trên Local để nạp cấu hình Proxy HCM `171.246.111.124` (hoặc copy dòng `proxy:` sang `config/secrets.yaml` trên VPS nếu muốn gắn Proxy cho VPS).
