# STATE HANDOFF (Zer0mem)
1. **Đã hoàn thành**: Xác thực đăng nhập thành công tài khoản `DEA209907000` vào sảnh thể thao `https://d.bong88.com` qua Proxy Dân cư Viettel HCM (`171.246.111.124`), tự động trích xuất AccessToken JWT (`MS2.at`) và số dư thực tế (`10,000.000 UT`).
2. **Trạng thái hiện tại**: Đã hoàn thiện toàn bộ luồng cược live thực tế (`get_ticket_quote` -> `/Betting/GetTickets` và `place_single_bet` -> `/Betting/ProcessBet`) theo đúng chuẩn API sportsbook Bong88 với bảo vệ trượt giá (`Slippage Guard`), chống rớt Proxy (`Proxy Killswitch`) và cắt lỗ tự động `-15%`.
3. **Task tiếp theo**: Để bắt đầu đặt tiền thật, chỉ cần đổi `mode: "live"` trong `config/settings.yaml` rồi chạy `python src/main.py`.
