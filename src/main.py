import argparse
import asyncio
from datetime import datetime
import os
import sys
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Đảm bảo import được package src khi chạy từ thư mục gốc
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.engine.analyzer import OddsConverter, SignalAnalyzer
from src.engine.matcher import MatchMatcher
from src.engine.risk_manager import RiskManager
from src.feeds.sharp_feed import SharpFeedClient
from src.feeds.viva88_client import Viva88Client

console = Console()


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def print_summary_table(risk_mgr: RiskManager, stats: dict) -> None:
    table = Table(title="\nBẢNG TỔNG KẾT VÉ ĐẶT GIẢ LẬP (PAPER TRADE VIVA88)")
    table.add_column("Mã Vé", style="cyan")
    table.add_column("Trận Đấu", style="white")
    table.add_column("Loại Kèo", style="magenta")
    table.add_column("Cửa Đặt (1 Cửa)", style="yellow")
    table.add_column("Odds (Dec/HK)", justify="right")
    table.add_column("Tỉ Lệ Thắng Thực", justify="right", style="green")
    table.add_column("+EV%", justify="right", style="green")
    table.add_column("Tiền Cược", justify="right")
    table.add_column("Kết Quả", justify="center")

    for t in risk_mgr.tickets:
        status_str = (
            f"[bold green]{t.status.value}[/bold green]"
            if t.status.value == "WON"
            else (
                f"[bold red]{t.status.value}[/bold red]"
                if t.status.value == "LOST"
                else f"[bold yellow]{t.status.value}[/bold yellow]"
            )
        )
        table.add_row(
            t.ticket_id,
            t.match_name,
            f"{t.phase.value} - {t.market_type.value}",
            t.selection,
            f"{t.odds_accepted:.2f} ({t.odds_accepted - 1.0:.2f})",
            f"{t.true_win_prob * 100:.1f}%",
            f"+{t.ev_percent:.2f}%",
            f"{t.stake:,.0f}",
            status_str,
        )

    console.print(table)
    console.print(
        f"[bold]Thống kê Paper Trade:[/bold] Tổng số mã: {stats['settled']} | "
        f"Thắng: [green]{stats['won']}[/green] - Thua: [red]{stats['lost']}[/red] "
        f"(Winrate: [bold green]{stats['win_rate']}%[/bold green]) | "
        f"Lãi ròng: [bold green]{stats['net_profit']:+,.2f}[/bold green] "
        f"(ROI: [bold yellow]{stats['roi']:+.2f}%[/bold yellow]) | "
        f"Số dư hiện tại: [bold cyan]{stats['bankroll']:,.2f}[/bold cyan]\n"
    )


async def run_continuous_bot(max_cycles: int = 0) -> None:
    cfg = load_config()
    poll_interval = float(cfg["sharp_feed"].get("poll_interval_sec", 5))
    sharp_client = SharpFeedClient(cfg)
    viva_client = Viva88Client(cfg)
    analyzer = SignalAnalyzer(cfg)
    risk_mgr = RiskManager(cfg)

    await viva_client.login()

    console.print(
        Panel.fit(
            f"[bold cyan]HỆ THỐNG AUTOBET 1 CỬA VIVA88 (CHẾ ĐỘ: {cfg['mode'].upper()})[/bold cyan]\n"
            f"• Vốn hiện tại: [bold green]{risk_mgr.bankroll:,.2f}[/bold green] | "
            f"Điều kiện nổ kèo: [yellow]Winrate thực >= {cfg['strategy']['min_win_prob']*100:.0f}%[/yellow] & "
            f"[yellow]+EV >= {cfg['strategy']['min_ev_percent']}%[/yellow]\n"
            f"• Quy tắc kỷ luật: [bold magenta]Chỉ đánh 1 cửa duy nhất mỗi trận/loại kèo (Không bù khi nhảy line)[/bold magenta]\n"
            f"• Chu kỳ quét: [bold white]{poll_interval:.0f} giây/lần[/bold white] | "
            f"Nhấn tổ hợp phím [bold red]Ctrl + C[/bold red] bất cứ lúc nào để dừng và xem bảng tổng kết.",
            border_style="cyan",
        )
    )

    tick = 0
    try:
        while True:
            if max_cycles > 0 and tick >= max_cycles:
                break

            now_str = datetime.now().strftime("%H:%M:%S")
            console.print(
                f"\n[bold white]━━━━━━━━━━━━━━━━ [Chu kỳ #{tick + 1} | {now_str}] ━━━━━━━━━━━━━━━━[/bold white]"
            )

            # BƯỚC 1: Quét dữ liệu 2 đầu
            sharp_lines, viva_lines = await asyncio.gather(
                sharp_client.fetch_sharp_lines(tick=tick),
                viva_client.fetch_viva88_lines(tick=tick),
            )
            paired = MatchMatcher.pair_lines(sharp_lines, viva_lines)
            console.print(
                f"  [cyan]1. Quét & Ghép cặp:[/cyan] Thu thập {len(sharp_lines)} kèo Pinnacle & "
                f"{len(viva_lines)} kèo Viva88 -> Ghép khớp [bold]{len(paired)}[/bold] cặp trận."
            )

            # BƯỚC 2 & 3: Phân tích từng cặp & Đặt cược
            new_bets_this_tick = 0
            for match_key, s_line, v_line in paired:
                true_prob, fair_odds = analyzer.devig_power(
                    s_line.odds_dec, s_line.opposite_odds_dec
                )
                ev_pct = round(((true_prob * v_line.odds_dec) - 1.0) * 100.0, 2)
                viva_hk = OddsConverter.dec_to_hk(v_line.odds_dec)

                signal = analyzer.evaluate_pair(
                    sharp_line=s_line,
                    viva_line=v_line,
                    match_key=match_key,
                    current_bankroll=risk_mgr.bankroll,
                )

                # Nếu không đạt tiêu chuẩn Winrate hoặc +EV, in rõ lý do lọc bỏ
                if not signal:
                    console.print(
                        f"     [dim]• [BỎ QUA - KHÔNG ĐỦ ĐIỀU KIỆN] {v_line.home_team} vs {v_line.away_team} "
                        f"({v_line.market_type.value} {v_line.side} {v_line.line}) | "
                        f"Viva88 HK {viva_hk:.2f} | Winrate thực: {true_prob*100:.1f}% | EV: {ev_pct:+.2f}%[/dim]"
                    )
                    continue

                # Nếu đạt tiêu chuẩn nhưng trận này đã đánh 1 mã trước đó -> Khóa 1 cửa
                if not risk_mgr.can_place_bet(signal):
                    console.print(
                        f"     [yellow]• [ĐÃ KHÓA 1 CỬA - KHÔNG BÙ KÈO] {signal.home_team} vs {signal.away_team} "
                        f"({signal.market_type.value} {signal.side} {signal.line}) - Đã có vé trên trận này.[/yellow]"
                    )
                    continue

                # Đạt đủ điều kiện -> Gọi Autobet Viva88 (GetTicket -> Slippage Guard -> PlaceBet)
                ticket = await viva_client.place_single_bet(signal)
                risk_mgr.register_ticket(signal, ticket)
                new_bets_this_tick += 1
                console.print(
                    f"     [bold green]★ [NỔ LỆNH {ticket.ticket_id} - {signal.signal_type}][/bold green] "
                    f"[bold white]{ticket.match_name}[/bold white] | "
                    f"[cyan]{ticket.phase.value} {ticket.market_type.value}[/cyan] "
                    f"[bold yellow]{ticket.selection}[/bold yellow] @ Odds [bold]{ticket.odds_accepted:.2f}[/bold] "
                    f"(HK {signal.viva88_hk_odds:.2f}) | Winrate: [bold green]{signal.true_win_prob*100:.1f}%[/bold green] "
                    f"| EV: [bold green]+{signal.ev_percent:.2f}%[/bold green] | Cược: [bold]{ticket.stake:.0f}[/bold]"
                )

            # BƯỚC 4: Kết toán giả lập & Hiển thị trạng thái ví
            stats = risk_mgr.settle_simulation_tickets(rng_seed=42 + tick)
            console.print(
                f"  [cyan]2. Trạng thái Ví Paper Trade:[/cyan] Vé mới: [bold]{new_bets_this_tick}[/bold] | "
                f"Tổng vé đã vào: [bold]{stats['settled']}[/bold] "
                f"(Thắng: [green]{stats['won']}[/green] / Thua: [red]{stats['lost']}[/red] - "
                f"Winrate: [bold green]{stats['win_rate']}%[/bold green]) | "
                f"Lãi ròng: [bold green]{stats['net_profit']:+,.2f}[/bold green] | "
                f"Số dư: [bold cyan]{stats['bankroll']:,.2f}[/bold cyan]"
            )

            tick += 1
            if max_cycles > 0 and tick >= max_cycles:
                break

            console.print(
                f"  [dim]Đang đợi {poll_interval:.0f}s cho chu kỳ quét tiếp theo (Bấm Ctrl + C để dừng)...[/dim]"
            )
            await asyncio.sleep(poll_interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[bold yellow]>>> Đã nhận lệnh dừng (Ctrl + C) từ người dùng![/bold yellow]")

    final_stats = risk_mgr.settle_simulation_tickets(rng_seed=42)
    print_summary_table(risk_mgr, final_stats)
    console.print(f"[dim]Đã lưu toàn bộ lịch sử vé vào: {risk_mgr.ledger_path}[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Abitrade Viva88 Single-Sided Autobet")
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="Số chu kỳ chạy (Mặc định 0 = Chạy liên tục vĩnh viễn cho đến khi bấm Ctrl + C)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_continuous_bot(max_cycles=args.cycles))
    except KeyboardInterrupt:
        pass
