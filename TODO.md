# STATE HANDOFF (Zer0mem)
1. **Đã hoàn thành**: Xây dựng hệ thống Autobet 1 cửa Viva88 (`src/`, `config/settings.yaml`, `README.md`), kiểm thử Paper Trade thành công và đã push lên `https://github.com/nhancm/Abitrade.git` (nhánh `main`).
2. **Trạng thái hiện tại**: Đang chạy ở chế độ `simulation` (Paper Trade) với bộ lọc `Winrate >= 56%`, `+EV >= 3%`, `1/4 Kelly` và khóa chặt 1 cửa (`one_shot_per_market: true`).
3. **Task tiếp theo**: Tích hợp phiên đăng nhập HTTP/Socket thực tế của Viva88 (Member/Agent) để quét bảng kèo Saba thời gian thực và kết nối Telegram Bot báo tín hiệu.
