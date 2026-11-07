import argparse
import asyncio
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
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


def setup_logger(log_dir: str = "logs") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("abitrade")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            os.path.join(log_dir, "abitrade.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def append_audit_jsonl(record: dict, path: str = "logs/audit_metrics.jsonl") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_config(path: str = "config/settings.yaml", secrets_path: str = "config/secrets.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if os.path.exists(secrets_path):
        with open(secrets_path, "r", encoding="utf-8") as sf:
            secrets = yaml.safe_load(sf) or {}
            if "viva88" in secrets:
                cfg.setdefault("viva88", {}).update(secrets["viva88"])
    return cfg


def print_tickets_table(risk_mgr: RiskManager, stats: dict, title: str) -> None:
    table = Table(title=title)
    table.add_column("Mã Vé (SabaID)", style="cyan")
    table.add_column("Giải / Trận Đấu Thực Tế Viva88", style="white")
    table.add_column("Phút & Tỉ Số", justify="center", style="yellow")
    table.add_column("Cửa Đặt (1 Cửa)", style="magenta")
    table.add_column("Odds (HK)", justify="right")
    table.add_column("Winrate / +EV", justify="right", style="green")
    table.add_column("Tiền Cược", justify="right")
    table.add_column("Trạng Thái / Lãi Lỗ", justify="center")

    for t in risk_mgr.tickets:
        if t.status.value == "RUNNING":
            status_str = "[bold yellow]RUNNING (Chờ hết trận)[/bold yellow]"
        elif t.status.value in ("WON", "HALF_WON"):
            status_str = f"[bold green]{t.status.value} ({t.pnl:+,.1f})[/bold green]"
        elif t.status.value in ("LOST", "HALF_LOST"):
            status_str = f"[bold red]{t.status.value} ({t.pnl:+,.1f})[/bold red]"
        else:
            status_str = f"[bold white]{t.status.value}[/bold white]"

        live_info = f"{t.current_minute} | {t.current_score} (Góc {t.current_corners})"
        table.add_row(
            f"{t.ticket_id}\n[dim]Saba:{t.saba_id}[/dim]",
            f"[bold]{t.match_name}[/bold]\n[dim]{t.league}[/dim]",
            live_info,
            f"{t.phase.value} {t.market_type.value}\n[bold yellow]{t.selection}[/bold yellow] (Lúc đặt: {t.score_at_bet})",
            f"{t.odds_accepted:.2f}\n(HK {t.odds_accepted - 1.0:.2f})",
            f"{t.true_win_prob * 100:.1f}%\n+{t.ev_percent:.2f}%",
            f"{t.stake:,.0f}",
            status_str,
        )

    console.print(table)
    console.print(
        f"[bold]Tổng quan Ví Paper Trade:[/bold] "
        f"Vé đang chạy (RUNNING): [bold yellow]{stats['running_count']} vé ({stats['running_stake']:,.0f} đ)[/bold yellow] | "
        f"Đã kết toán FT: [bold]{stats['settled']}[/bold] "
        f"(Thắng: [green]{stats['won']}[/green] / Thua: [red]{stats['lost']}[/red] / Hòa: {stats['push']} - "
        f"Winrate: [bold green]{stats['win_rate']}%[/bold green]) | "
        f"Lãi đã chốt: [bold green]{stats['net_profit']:+,.2f}[/bold green] | "
        f"Số dư khả dụng: [bold cyan]{stats['available_bankroll']:,.2f}[/bold cyan] "
        f"(Tổng tài sản: [bold white]{stats['total_equity']:,.2f}[/bold white])\n"
    )


async def run_continuous_bot(max_cycles: int = 0) -> None:
    cfg = load_config()
    logger = setup_logger()
    poll_interval = float(cfg["sharp_feed"].get("poll_interval_sec", 5))
    sharp_client = SharpFeedClient(cfg)
    viva_client = Viva88Client(cfg)
    analyzer = SignalAnalyzer(cfg)
    risk_mgr = RiskManager(cfg)

    await viva_client.login()
    init_stats = risk_mgr.get_portfolio_stats()
    logger.info(
        f"START REALTIME BOT | mode={cfg['mode']} | account={viva_client.logged_in_user} | "
        f"session={viva_client.session_token} | available_bankroll={init_stats['available_bankroll']:.2f}"
    )

    console.print(
        Panel.fit(
            f"[bold cyan]HỆ THỐNG AUTOBET 1 CỬA VIVA88 — DỮ LIỆU TRẬN ĐẤU THỰC TẾ SABA SPORTS[/bold cyan]\n"
            f"• Tài khoản Viva88: [bold yellow]{viva_client.logged_in_user}[/bold yellow] (Session: [green]{viva_client.session_token}[/green])\n"
            f"• Số dư khả dụng: [bold green]{init_stats['available_bankroll']:,.2f}[/bold green] "
            f"(Đang treo RUNNING: [yellow]{init_stats['running_count']} vé / {init_stats['running_stake']:,.0f}[/yellow])\n"
            f"• Điều kiện nổ kèo: [yellow]Winrate thực >= {cfg['strategy']['min_win_prob']*100:.0f}%[/yellow] & "
            f"[yellow]+EV >= {cfg['strategy']['min_ev_percent']}%[/yellow] (Chỉ đánh 1 cửa / Không bù kèo)\n"
            f"• Cơ chế kết toán: [bold green]Theo dõi tỉ số & phạt góc thực tế trên Viva88 — Chỉ chốt tiền khi trận đấu FT (hoặc nổ đủ Tài sớm)[/bold green]\n"
            f"• Nhấn [bold red]Ctrl + C[/bold red] bất cứ lúc nào để dừng và xem bảng chi tiết.",
            border_style="cyan",
        )
    )

    tick = 0
    stop_event = asyncio.Event()

    def _on_sigint(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _on_sigint)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_sigint)

    try:
        while not stop_event.is_set():
            if max_cycles > 0 and tick >= max_cycles:
                break

            now_str = datetime.now().strftime("%H:%M:%S")
            console.print(
                f"\n[bold white]━━━━━━━━━━━━━━━━ [Chu kỳ #{tick + 1} | {now_str}] ━━━━━━━━━━━━━━━━[/bold white]"
            )

            # BƯỚC 1: Lấy danh sách trận đấu THẬT đang mở trên sảnh Saba của Viva88
            viva_lines = await viva_client.fetch_viva88_lines(tick=tick)
            if stop_event.is_set():
                break
            saba_matches = list(viva_client.live_match_map.values())
            sharp_lines = sharp_client.build_sharp_lines_from_real_matches(
                saba_matches=saba_matches, viva_lines=viva_lines, tick=tick
            )
            paired = MatchMatcher.pair_lines(sharp_lines, viva_lines)
            live_count = sum(1 for m in saba_matches if m.get("Status") in (10, 20, 30))

            console.print(
                f"  [cyan]1. Quét Bảng Kèo Viva88 Thực Tế:[/cyan] Tổng {len(saba_matches)} trận Saba hôm nay "
                f"([bold yellow]{live_count} trận đang LIVE[/bold yellow]) -> Phân tích [bold]{len(paired)}[/bold] dòng kèo."
            )

            # BƯỚC 2 & 3: Phân tích từng cặp, sắp xếp ưu tiên kèo có +EV cao nhất mỗi trận & Đặt cược
            new_bets_this_tick = 0
            skipped_low_ev = 0
            skipped_locked = 0

            candidate_signals = []
            for match_key, s_line, v_line in paired:
                signal = analyzer.evaluate_pair(
                    sharp_line=s_line,
                    viva_line=v_line,
                    match_key=match_key,
                    current_bankroll=risk_mgr.bankroll,
                )
                if not signal:
                    skipped_low_ev += 1
                else:
                    candidate_signals.append(signal)

            # Ưu tiên chọn kèo có lợi nhuận kỳ vọng (+EV%) cao nhất trước
            candidate_signals.sort(key=lambda s: s.ev_percent, reverse=True)

            for signal in candidate_signals:
                if stop_event.is_set():
                    break
                if not risk_mgr.can_place_bet(signal):
                    skipped_locked += 1
                    logger.info(
                        f"CYCLE #{tick+1} | SKIP_LOCKED_1SHOT | Saba:{signal.saba_id} | {signal.home_team} vs {signal.away_team} | "
                        f"{signal.market_type.value} {signal.side} {signal.line}"
                    )
                    console.print(
                        f"     [yellow]• [ĐÃ KHÓA 1 CỬA] (Saba:{signal.saba_id}) [{signal.league}] "
                        f"{signal.home_team} vs {signal.away_team} ({signal.minute}, Tỉ số {signal.score_home}-{signal.score_away}) "
                        f"— Đã có vé trên trận này.[/yellow]"
                    )
                    continue

                ticket = await viva_client.place_single_bet(signal)
                risk_mgr.register_ticket(signal, ticket)
                new_bets_this_tick += 1
                logger.info(
                    f"CYCLE #{tick+1} | BET_PLACED_RUNNING | id={ticket.ticket_id} | saba_id={ticket.saba_id} | "
                    f"[{ticket.league}] {ticket.match_name} ({ticket.current_minute}, score={ticket.current_score}, crn={ticket.current_corners}) | "
                    f"{ticket.phase.value} {ticket.market_type.value} {ticket.selection} @ Dec {ticket.odds_accepted:.2f} (HK {signal.viva88_hk_odds:.2f}) | "
                    f"win_prob={signal.true_win_prob*100:.1f}% | ev=+{signal.ev_percent:.2f}% | stake={ticket.stake:.0f}"
                )
                console.print(
                    f"     [bold green]★ [VÀO VÉ {ticket.ticket_id} -> RUNNING][/bold green] "
                    f"(Saba:[bold]{ticket.saba_id}[/bold]) [{ticket.league}] [bold white]{ticket.match_name}[/bold white] "
                    f"([yellow]{ticket.current_minute} | Tỉ số {ticket.current_score} | Góc {ticket.current_corners}[/yellow]) -> "
                    f"[cyan]{ticket.phase.value} {ticket.market_type.value}[/cyan] [bold yellow]{ticket.selection}[/bold yellow] "
                    f"@ HK [bold]{signal.viva88_hk_odds:.2f}[/bold] | Winrate: [bold green]{signal.true_win_prob*100:.1f}%[/bold green] "
                    f"| EV: [bold green]+{signal.ev_percent:.2f}%[/bold green] | Cược: [bold]{ticket.stake:.0f}[/bold]"
                )

            # BƯỚC 4: Cập nhật tỉ số trực tiếp & Chốt các vé đã kết thúc (FT hoặc nổ đủ Tài sớm)
            stats = risk_mgr.update_and_settle_real_tickets(viva_client.live_match_map)
            logger.info(
                f"CYCLE #{tick+1} | SUMMARY | saba_live={live_count} paired={len(paired)} new_bets={new_bets_this_tick} "
                f"running={stats['running_count']} settled={stats['settled']} won={stats['won']} lost={stats['lost']} "
                f"net_profit={stats['net_profit']:+.2f} avail_bankroll={stats['available_bankroll']:.2f}"
            )
            append_audit_jsonl(
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "cycle": tick + 1,
                    "saba_live_matches": live_count,
                    "paired_count": len(paired),
                    "new_bets": new_bets_this_tick,
                    "running_tickets": stats["running_count"],
                    "running_stake": stats["running_stake"],
                    "settled": stats["settled"],
                    "won": stats["won"],
                    "lost": stats["lost"],
                    "win_rate": stats["win_rate"],
                    "net_profit": stats["net_profit"],
                    "available_bankroll": stats["available_bankroll"],
                    "total_equity": stats["total_equity"],
                }
            )

            console.print(
                f"  [cyan]2. Theo dõi Vé Thực Tế:[/cyan] Vé mới vào: [bold green]{new_bets_this_tick}[/bold green] | "
                f"Đang treo chờ hết trận (RUNNING): [bold yellow]{stats['running_count']} vé ({stats['running_stake']:,.0f} đ)[/bold yellow] | "
                f"Đã chốt FT: [bold]{stats['settled']}[/bold] (Thắng: [green]{stats['won']}[/green] / Thua: [red]{stats['lost']}[/red]) | "
                f"Số dư khả dụng: [bold cyan]{stats['available_bankroll']:,.2f}[/bold cyan]"
            )

            tick += 1
            if max_cycles > 0 and tick >= max_cycles:
                break

            console.print(
                f"  [dim]Đang theo dõi tỉ số trực tiếp trên Viva88 (Cập nhật sau {poll_interval:.0f}s — Bấm Ctrl + C để dừng)...[/dim]"
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("STOP BOT | User pressed Ctrl+C")
        console.print("\n[bold yellow]>>> Đã dừng chương trình an toàn (Ctrl + C)![/bold yellow]")
        await viva_client.close()
        final_stats = risk_mgr.update_and_settle_real_tickets(viva_client.live_match_map)
        print_tickets_table(
            risk_mgr,
            final_stats,
            title="\nBẢNG THEO DÕI VÉ ĐANG CHẠY (RUNNING) & ĐÃ KẾT TOÁN TRÊN VIVA88",
        )
        console.print(
            f"[dim]Đã lưu sổ cái: {risk_mgr.ledger_path} | File log: logs/abitrade.log & logs/audit_metrics.jsonl[/dim]"
        )


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
