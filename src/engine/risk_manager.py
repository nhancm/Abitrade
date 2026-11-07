import json
import os
import random
from typing import Dict, List, Set
from src.models import BetStatus, BetTicket, ValueSignal


class RiskManager:
    """
    Quản lý rủi ro & Kỷ luật đánh 1 cửa (One-Shot Single-Sided Rule):
    - Khóa vĩnh viễn cặp (match_key, market_type) ngay sau khi đã vào 1 lệnh thành công.
    - Tuyệt đối không nhồi thêm, không bù/xả khi kèo nhảy tỉ lệ mới.
    - Lưu sổ cái (Ledger) và mô phỏng kết quả thắng/thua theo xác suất thực cho chế độ Paper Trade.
    """

    def __init__(self, config: dict, ledger_path: str = "data/paper_ledger.json"):
        self.cfg = config
        self.bankroll = float(config["bankroll"]["initial_capital"])
        self.one_shot_only = config["strategy"].get("one_shot_per_market", True)
        self.ledger_path = ledger_path
        self.locked_markets: Set[str] = set()
        self.tickets: List[BetTicket] = []
        self._load_ledger()

    def _load_ledger(self) -> None:
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.bankroll = data.get("bankroll", self.bankroll)
                self.locked_markets = set(data.get("locked_markets", []))
        except Exception:
            pass

    def save_ledger(self) -> None:
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        payload = {
            "bankroll": round(self.bankroll, 2),
            "locked_markets": list(self.locked_markets),
            "total_tickets": len(self.tickets),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "match_name": t.match_name,
                    "phase": t.phase.value,
                    "market": t.market_type.value,
                    "selection": t.selection,
                    "odds": t.odds_accepted,
                    "win_prob": round(t.true_win_prob * 100, 1),
                    "ev_pct": t.ev_percent,
                    "stake": t.stake,
                    "status": t.status.value,
                    "note": t.note,
                }
                for t in self.tickets
            ],
        }
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def lock_key(self, signal: ValueSignal) -> str:
        """Khóa theo Trận + Loại kèo (HDP / OU / CORNER) để không bao giờ đánh lệnh thứ 2 khi nhảy line."""
        return f"{signal.match_key}::{signal.market_type.value}"

    def can_place_bet(self, signal: ValueSignal) -> bool:
        if self.one_shot_only and self.lock_key(signal) in self.locked_markets:
            return False
        if signal.recommended_stake > self.bankroll:
            return False
        return True

    def register_ticket(self, signal: ValueSignal, ticket: BetTicket) -> None:
        self.tickets.append(ticket)
        if ticket.status in (BetStatus.RUNNING, BetStatus.WON, BetStatus.LOST):
            self.locked_markets.add(self.lock_key(signal))
        self.save_ledger()

    def settle_simulation_tickets(self, rng_seed: int = None) -> Dict[str, float]:
        """
        Mô phỏng kết toán các vé RUNNING trong chế độ Paper Trade dựa trên True Win Probability
        để kiểm chứng lợi nhuận thực tế khi đánh số lượng kèo lớn.
        """
        rng = random.Random(rng_seed)
        won_count = 0
        lost_count = 0
        total_staked = 0.0
        net_profit = 0.0

        for t in self.tickets:
            if t.status == BetStatus.REJECTED:
                continue
            total_staked += t.stake
            if t.status == BetStatus.RUNNING:
                if rng.random() <= t.true_win_prob:
                    t.status = BetStatus.WON
                    profit = t.stake * (t.odds_accepted - 1.0)
                    self.bankroll += profit
                    net_profit += profit
                    won_count += 1
                else:
                    t.status = BetStatus.LOST
                    self.bankroll -= t.stake
                    net_profit -= t.stake
                    lost_count += 1
            elif t.status == BetStatus.WON:
                won_count += 1
                net_profit += t.stake * (t.odds_accepted - 1.0)
            elif t.status == BetStatus.LOST:
                lost_count += 1
                net_profit -= t.stake

        self.save_ledger()
        settled = won_count + lost_count
        win_rate = (won_count / settled * 100.0) if settled > 0 else 0.0
        roi = (net_profit / total_staked * 100.0) if total_staked > 0 else 0.0
        return {
            "settled": settled,
            "won": won_count,
            "lost": lost_count,
            "win_rate": round(win_rate, 2),
            "total_staked": round(total_staked, 2),
            "net_profit": round(net_profit, 2),
            "roi": round(roi, 2),
            "bankroll": round(self.bankroll, 2),
        }
