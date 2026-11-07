import json
import os
from typing import Dict, List, Set, Tuple
from src.models import BetStatus, BetTicket, MarketType, MatchPhase, ValueSignal


class RiskManager:
    """
    Quản lý rủi ro, Kỷ luật 1 cửa (One-Shot) & Kết toán theo Kết Quả Thực Tế trên Viva88:
    - Khi đặt cược: Vé ở trạng thái `RUNNING` (Đang chạy), số tiền cược (`stake`) được tạm khóa khỏi số dư khả dụng.
    - Mỗi chu kỳ quét: Cập nhật tỉ số, phạt góc, phút thi đấu thực tế từ Viva88 vào từng vé `RUNNING`.
    - Chỉ chốt kết quả (`WON`, `HALF_WON`, `PUSH`, `HALF_LOST`, `LOST`) khi:
      1. Trận đấu trên Viva88 kết thúc 90 phút (`Status == 50` - FT), HOẶC
      2. Vé đánh cửa `OVER` (Tài bàn / Tài góc) đã nổ vượt mốc kèo ngay trong trận (Ăn sớm - Early Hit).
    """

    def __init__(self, config: dict, ledger_path: str = "data/paper_ledger.json"):
        self.cfg = config
        self.initial_capital = float(config["bankroll"]["initial_capital"])
        self.bankroll = self.initial_capital  # Số dư khả dụng (đã trừ tiền các vé đang RUNNING)
        self.one_shot_only = config["strategy"].get("one_shot_per_market", True)
        self.one_bet_per_match = config["strategy"].get("one_bet_per_match", True)
        self.max_running_tickets = int(config["strategy"].get("max_running_tickets", 10))
        self.ledger_path = ledger_path
        self.locked_markets: Set[str] = set()
        self.tickets: List[BetTicket] = []
        self.newly_settled: List[BetTicket] = []
        self._load_ledger()

    def _load_ledger(self) -> None:
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Chỉ nạp lại sổ cái nếu là dữ liệu phiên bản mới (có saba_id thực tế)
                if data.get("schema_version") == 2:
                    self.bankroll = float(data.get("bankroll", self.initial_capital))
                    self.locked_markets = set(data.get("locked_markets", []))
                    for item in data.get("tickets", []):
                        self.tickets.append(
                            BetTicket(
                                ticket_id=item["ticket_id"],
                                match_id=item["match_id"],
                                saba_id=item["saba_id"],
                                match_key=item["match_key"],
                                league=item.get("league", ""),
                                match_name=item["match_name"],
                                phase=MatchPhase(item["phase"]),
                                market_type=MarketType(item["market"]),
                                line=float(item["line"]),
                                side=item["side"],
                                selection=item["selection"],
                                odds_requested=float(item["odds"]),
                                odds_accepted=float(item["odds"]),
                                true_win_prob=float(item["win_prob"]) / 100.0,
                                ev_percent=float(item["ev_pct"]),
                                stake=float(item["stake"]),
                                status=BetStatus(item["status"]),
                                score_at_bet=item.get("score_at_bet", "0-0"),
                                current_score=item.get("current_score", "0-0"),
                                current_corners=item.get("current_corners", "0-0"),
                                current_minute=item.get("current_minute", "Pre"),
                                pnl=float(item.get("pnl", 0.0)),
                                note=item.get("note", ""),
                            )
                        )
                        self.locked_markets.add(f"MATCH::{item['match_id']}")
        except Exception:
            pass

    def save_ledger(self) -> None:
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        payload = {
            "schema_version": 2,
            "bankroll": round(self.bankroll, 2),
            "locked_markets": list(self.locked_markets),
            "total_tickets": len(self.tickets),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "match_id": t.match_id,
                    "saba_id": t.saba_id,
                    "match_key": t.match_key,
                    "league": t.league,
                    "match_name": t.match_name,
                    "phase": t.phase.value,
                    "market": t.market_type.value,
                    "line": t.line,
                    "side": t.side,
                    "selection": t.selection,
                    "odds": t.odds_accepted,
                    "win_prob": round(t.true_win_prob * 100, 1),
                    "ev_pct": t.ev_percent,
                    "stake": t.stake,
                    "status": t.status.value,
                    "score_at_bet": t.score_at_bet,
                    "current_score": t.current_score,
                    "current_corners": t.current_corners,
                    "current_minute": t.current_minute,
                    "pnl": round(t.pnl, 2),
                    "note": t.note,
                }
                for t in self.tickets
            ],
        }
        tmp_path = f"{self.ledger_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.ledger_path)

    def lock_key(self, signal: ValueSignal) -> str:
        if self.one_bet_per_match:
            return f"MATCH::{signal.match_id}"
        return f"{signal.match_id}::{signal.market_type.value}"

    def is_match_locked(self, signal: ValueSignal) -> bool:
        if not self.one_shot_only:
            return False
        if self.lock_key(signal) in self.locked_markets:
            return True
        if f"{signal.match_id}::{signal.market_type.value}" in self.locked_markets:
            return True
        return False

    def is_max_running_reached(self) -> bool:
        running_now = sum(1 for t in self.tickets if t.status == BetStatus.RUNNING)
        return running_now >= self.max_running_tickets

    def can_place_bet(self, signal: ValueSignal) -> bool:
        if self.is_max_running_reached():
            return False
        if self.is_match_locked(signal):
            return False
        if signal.recommended_stake > self.bankroll:
            return False
        return True

    def register_ticket(self, signal: ValueSignal, ticket: BetTicket) -> None:
        self.tickets.append(ticket)
        if ticket.status == BetStatus.RUNNING:
            self.bankroll -= ticket.stake  # Tạm khóa tiền cược khi vé đang RUNNING
            self.locked_markets.add(self.lock_key(signal))
        self.save_ledger()

    @staticmethod
    def _grade_asian_line(diff: float, odds_dec: float, stake: float) -> Tuple[BetStatus, float]:
        """
        Tính kết quả chuẩn kèo Châu Á (bao gồm kèo đồng banh, 0.25, 0.5, 0.75, 1.0):
        `diff` là độ chênh lệch bàn thắng/góc của cửa đặt so với mốc kèo.
        Trả về: (BetStatus, net_pnl)
        """
        hk_odds = odds_dec - 1.0
        if diff >= 0.5:
            return BetStatus.WON, round(stake * hk_odds, 2)
        if abs(diff - 0.25) < 1e-4:
            return BetStatus.HALF_WON, round(stake * hk_odds * 0.5, 2)
        if abs(diff) < 1e-4:
            return BetStatus.PUSH, 0.0
        if abs(diff + 0.25) < 1e-4:
            return BetStatus.HALF_LOST, round(-stake * 0.5, 2)
        return BetStatus.LOST, round(-stake, 2)

    def update_and_settle_real_tickets(self, live_match_map: Dict[str, dict]) -> Dict[str, float]:
        """
        Đối chiếu từng vé `RUNNING` với bảng tỉ số thực tế từ Viva88:
        - Nếu trận đấu chưa xong (`Status != 50`), giữ nguyên `RUNNING` (trừ khi kèo OVER đã đủ bàn ăn sớm).
        - Nếu trận đấu đã kết thúc (`Status == 50` - FT), tự động chấm Thắng/Thua và hoàn vốn + lãi về ví.
        """
        self.newly_settled = []
        for t in self.tickets:
            if t.status != BetStatus.RUNNING:
                continue

            m = live_match_map.get(t.match_id)
            if not m:
                continue

            h = m.get("HomeTeam", {})
            a = m.get("AwayTeam", {})
            st = m.get("Status", 0)
            mt = m.get("MatchTime")
            sh = int(h.get("FullScore", 0))
            sa = int(a.get("FullScore", 0))
            ch = int(h.get("CornerKick", 0))
            ca = int(a.get("CornerKick", 0))

            t.current_score = f"{sh}-{sa}"
            t.current_corners = f"{ch}-{ca}"
            if st == 10:
                t.current_minute = f"1H {mt or 1}'"
            elif st == 20:
                t.current_minute = "HT"
            elif st == 30:
                t.current_minute = f"2H {mt or 46}'"
            elif st == 50:
                t.current_minute = "FT"

            # Giải phóng vé PREMATCH cũ chưa đá (Status == 0) hoặc trận bị hoãn/hủy (Status == 60) để không chiếm slot RUNNING
            if st in (0, 60):
                t.status = BetStatus.PUSH
                t.pnl = 0.0
                self.bankroll += t.stake
                t.note = "Hoàn tiền PUSH (Giải phóng vé Pre-match chưa đá / Trận hoãn để mở slot RUNNING)"
                self.newly_settled.append(t)
                continue

            # Kiểm tra điều kiện VAR hủy bàn thắng (Khi tổng bàn hiện tại thấp hơn tổng bàn lúc vào vé) -> Saba Void/PUSH vé
            total_goals = sh + sa
            total_corners = ch + ca
            try:
                bet_sh, bet_sa = [int(x) for x in t.score_at_bet.split("-")]
            except Exception:
                bet_sh, bet_sa = 0, 0
            if total_goals < (bet_sh + bet_sa):
                t.status = BetStatus.PUSH
                t.pnl = 0.0
                self.bankroll += t.stake
                t.note = "Hoàn tiền PUSH (Trọng tài VAR hủy bàn thắng lúc đặt vé)"
                self.newly_settled.append(t)
                continue

            # Kiểm tra điều kiện ăn sớm (Early Settlement) cho cửa OVER ngay trong trận
            early_over_win = False
            if t.market_type == MarketType.OU and t.side == "OVER" and (total_goals - t.line) >= 0.5:
                early_over_win = True
            elif t.market_type == MarketType.CORNER_OU and t.side == "OVER" and (total_corners - t.line) >= 0.5:
                early_over_win = True

            # Nếu trận đấu bị hoãn/hủy (Status == 60) -> Hoàn tiền PUSH
            if st == 60:
                t.status = BetStatus.PUSH
                t.pnl = 0.0
                self.bankroll += t.stake
                t.note = "Hoàn tiền (Trận đấu hoãn/hủy)"
                self.newly_settled.append(t)
                continue

            # Chỉ kết toán khi trận đấu đã FT (Status == 50) hoặc cửa OVER đã nổ đủ bàn ăn sớm
            if st != 50 and not early_over_win:
                continue

            if t.market_type == MarketType.OU:
                diff = (total_goals - t.line) if t.side == "OVER" else (t.line - total_goals)
            elif t.market_type == MarketType.CORNER_OU:
                diff = (total_corners - t.line) if t.side == "OVER" else (t.line - total_corners)
            elif t.market_type == MarketType.HDP:
                # Trong Saba Sports, kèo Chấp Live (Running Ball) tính hiệu số bàn thắng kể từ lúc đặt cược!
                try:
                    bet_sh, bet_sa = [int(x) for x in t.score_at_bet.split("-")]
                except Exception:
                    bet_sh, bet_sa = 0, 0
                net_home_goals = (sh - bet_sh) - (sa - bet_sa) if t.phase == MatchPhase.LIVE else (sh - sa)
                diff = (net_home_goals + t.line) if t.side == "HOME" else (-net_home_goals + t.line)
            else:
                diff = (ch - ca + t.line) if t.side == "HOME" else (ca - ch + t.line)

            new_status, pnl = self._grade_asian_line(diff, t.odds_accepted, t.stake)
            t.status = new_status
            t.pnl = pnl
            # Hoàn lại tiền gốc + Lãi/Lỗ vào số dư khả dụng
            self.bankroll += (t.stake + pnl)
            t.note = "Nổ Tài sớm ngay trong trận!" if (early_over_win and st != 50) else "Đã kết toán FT (Hết trận)"
            self.newly_settled.append(t)

        self.save_ledger()
        return self.get_portfolio_stats()

    def get_portfolio_stats(self) -> Dict[str, float]:
        running_tickets = [t for t in self.tickets if t.status == BetStatus.RUNNING]
        settled_tickets = [
            t for t in self.tickets if t.status in (BetStatus.WON, BetStatus.HALF_WON, BetStatus.PUSH, BetStatus.HALF_LOST, BetStatus.LOST)
        ]
        won_count = sum(1 for t in settled_tickets if t.status in (BetStatus.WON, BetStatus.HALF_WON))
        lost_count = sum(1 for t in settled_tickets if t.status in (BetStatus.LOST, BetStatus.HALF_LOST))
        push_count = sum(1 for t in settled_tickets if t.status == BetStatus.PUSH)

        running_stake = sum(t.stake for t in running_tickets)
        settled_stake = sum(t.stake for t in settled_tickets)
        net_profit = sum(t.pnl for t in settled_tickets)

        decisive = won_count + lost_count
        win_rate = (won_count / decisive * 100.0) if decisive > 0 else 0.0
        roi = (net_profit / settled_stake * 100.0) if settled_stake > 0 else 0.0
        total_equity = self.bankroll + running_stake

        return {
            "running_count": len(running_tickets),
            "running_stake": round(running_stake, 2),
            "settled": len(settled_tickets),
            "won": won_count,
            "lost": lost_count,
            "push": push_count,
            "win_rate": round(win_rate, 2),
            "net_profit": round(net_profit, 2),
            "roi": round(roi, 2),
            "available_bankroll": round(self.bankroll, 2),
            "total_equity": round(total_equity, 2),
        }
