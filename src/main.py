import asyncio
import os
import sys
import yaml
from rich.console import Console
from rich.table import Table

# Đảm bảo import được package src khi chạy từ thư mục gốc
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.engine.analyzer import SignalAnalyzer
from src.engine.matcher import MatchMatcher
from src.engine.risk_manager import RiskManager
from src.feeds.sharp_feed import SharpFeedClient
from src.feeds.viva88_client import Viva88Client

console = Console()


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run_simulation_cycles(cycles: int = 2) -> None:
    cfg = load_config()
    sharp_client = SharpFeedClient(cfg)
    viva_client = Viva88Client(cfg)
    analyzer = SignalAnalyzer(cfg)
    risk_mgr = RiskManager(cfg)

    await viva_client.login()
    console.print(
        f"[bold cyan]=== KHỞI ĐỘNG HỆ THỐNG AUTOBET VIVA88 (CHẾ ĐỘ: {cfg['mode'].upper()}) ===[/bold cyan]"
    )
    console.print(
        f"Vốn khởi điểm: [bold green]{risk_mgr.bankroll:,.2f}[/bold green] | "
        f"Lọc Winrate: [yellow]>={cfg['strategy']['min_win_prob']*100:.0f}%[/yellow] | "
        f"Lọc +EV: [yellow]>={cfg['strategy']['min_ev_percent']}%[/yellow] | "
        f"Kỷ luật: [bold magenta]Chỉ đánh 1 cửa / Không bù kèo[/bold magenta]\n"
    )

    for tick in range(cycles):
        console.print(f"[bold white]>>> Chu kỳ quét #{tick + 1}...[/bold white]")
        sharp_lines, viva_lines = await asyncio.gather(
            sharp_client.fetch_sharp_lines(tick=tick),
            viva_client.fetch_viva88_lines(tick=tick),
        )

        paired = MatchMatcher.pair_lines(sharp_lines, viva_lines)
        for match_key, s_line, v_line in paired:
            signal = analyzer.evaluate_pair(
                sharp_line=s_line,
                viva_line=v_line,
                match_key=match_key,
                current_bankroll=risk_mgr.bankroll,
            )
            if not signal:
                continue

            if not risk_mgr.can_place_bet(signal):
                console.print(
                    f"  [dim][SKIP - ĐÃ KHÓA 1 CỬA] {signal.home_team} vs {signal.away_team} "
                    f"({signal.market_type.value} {signal.side} {signal.line}) - Không vào thêm lệnh thứ 2.[/dim]"
                )
                continue

            ticket = await viva_client.place_single_bet(signal)
            risk_mgr.register_ticket(signal, ticket)
            console.print(
                f"  [bold green][NỔ LỆNH {ticket.ticket_id}][/bold green] "
                f"{ticket.match_name} | [cyan]{ticket.phase.value} {ticket.market_type.value}[/cyan] "
                f"[bold yellow]{ticket.selection}[/bold yellow] @ Odds [bold]{ticket.odds_accepted:.2f}[/bold] "
                f"(HK {signal.viva88_hk_odds:.2f}) | Winrate thực: [bold green]{signal.true_win_prob*100:.1f}%[/bold green] "
                f"| EV: [bold green]+{signal.ev_percent:.2f}%[/bold green] | Cược: [bold]{ticket.stake:.0f}[/bold]"
            )

    # Hiển thị bảng tổng kết các vé đã vào và mô phỏng kết toán
    stats = risk_mgr.settle_simulation_tickets(rng_seed=42)
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
            else f"[bold red]{t.status.value}[/bold red]"
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
        f"Số dư mới: [bold cyan]{stats['bankroll']:,.2f}[/bold cyan]"
    )


if __name__ == "__main__":
    asyncio.run(run_simulation_cycles(cycles=2))
