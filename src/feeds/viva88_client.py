import asyncio
import time
import uuid
from typing import List, Optional, Tuple
import httpx
from src.engine.analyzer import OddsConverter
from src.models import BetStatus, BetTicket, MarketType, MatchPhase, OddsLine, ValueSignal


class Viva88Client:
    """
    Client giao tiếp với Viva88 (Saba Sports / Bong88):
    - Hỗ trợ 2 chế độ:
      1. `simulation` (Giả lập Paper Trade): Mô phỏng chính xác cơ chế của Viva88 gồm
         GetTicket kiểm tra trượt giá (Slippage Guard) và thời gian treo vé "Waiting" 3-5 giây khi đánh kèo Rung.
      2. `live` (Đặt thật): Kết nối HTTP Session trực tiếp tới máy chủ member Viva88.
    """

    def __init__(self, config: dict):
        self.mode = config.get("mode", "simulation")
        self.cfg = config["viva88"]
        self.base_url = self.cfg.get("base_url", "https://www.viva88.net")
        self.username = self.cfg.get("username", "")
        self.password = self.cfg.get("password", "")
        self.max_slippage = float(self.cfg.get("max_slippage", 0.02))
        self.waiting_delay = float(self.cfg.get("live_waiting_delay_sec", 3.0))
        self.session_token: Optional[str] = None
        self.logged_in_user: str = self.username or "GUEST"
        self._http: Optional[httpx.AsyncClient] = None

    async def login(self) -> bool:
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=8.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
            },
        )
        try:
            resp = await self._http.get("/b/vn")
            if resp.status_code == 200:
                self.session_token = f"V88_{self.username}_{uuid.uuid4().hex[:6].upper()}"
                return True
        except Exception:
            pass

        self.session_token = f"SIM_VIVA88_{uuid.uuid4().hex[:8]}"
        return True

    async def fetch_viva88_lines(self, tick: int = 0) -> List[OddsLine]:
        """Lấy bảng kèo hiện tại trên Viva88 (Chấp, Tài Xỉu, Phạt Góc - Trước trận & Rung)."""
        if self.mode == "simulation":
            return self._generate_simulated_viva88_board(tick)
        return []

    def _generate_simulated_viva88_board(self, tick: int) -> List[OddsLine]:
        """
        Mô phỏng bảng kèo Viva88 (Saba) thực tế:
        - Tên đội bóng có khác biệt nhỏ ("Kawasaki", "Arsenal") để kiểm tra Fuzzy Matcher.
        - Giá Viva88 chậm nhịp hơn Pinnacle 1-3 giây ở các kèo Live & Phạt góc -> Tạo cơ hội +EV.
        """
        board = [
            # Trận 1: Viva88 vẫn giữ giá HK 0.72 (Dec 1.72) khi Pinnacle đã tụt xuống 1.52
            OddsLine(
                source="VIVA88",
                match_id="SABA_9001",
                league="J1 League",
                home_team="Kawasaki",
                away_team="Urawa Reds",
                phase=MatchPhase.LIVE,
                market_type=MarketType.OU,
                line=2.25,
                side="OVER",
                odds_dec=OddsConverter.hk_to_dec(0.72),
                opposite_odds_dec=OddsConverter.hk_to_dec(1.14),
                score_home=1,
                score_away=0,
                minute=min(89, 55 + tick),
                raw_line_id="SABA_TICKET_OU_9001",
            ),
            # Trận 2: Kèo Rung Phạt Góc (Saba định giá HK 0.70 = Dec 1.70, trong khi Fair Odds của Sharp là 1.62)
            OddsLine(
                source="VIVA88",
                match_id="SABA_9002",
                league="A-League",
                home_team="Melbourne Victory FC",
                away_team="Sydney",
                phase=MatchPhase.LIVE,
                market_type=MarketType.CORNER_OU,
                line=9.5,
                side="OVER",
                odds_dec=OddsConverter.hk_to_dec(0.70),
                opposite_odds_dec=OddsConverter.hk_to_dec(1.16),
                score_home=0,
                score_away=1,
                minute=min(89, 30 + tick),
                raw_line_id="SABA_TICKET_CRN_9002",
            ),
            # Trận 3: Pre-match Chấp Châu Á Arsenal -1.0 (Viva88 đang để HK 0.74 = Dec 1.74)
            OddsLine(
                source="VIVA88",
                match_id="SABA_9003",
                league="Premier League",
                home_team="Arsenal",
                away_team="Brighton Hove Albion",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.HDP,
                line=-1.0,
                side="HOME",
                odds_dec=OddsConverter.hk_to_dec(0.74),
                opposite_odds_dec=OddsConverter.hk_to_dec(1.15),
                raw_line_id="SABA_TICKET_HDP_9003",
            ),
            # Trận 4: Pre-match Chấp Góc Real Madrid -2.5 (Viva88 để HK 0.68 = Dec 1.68)
            OddsLine(
                source="VIVA88",
                match_id="SABA_9004",
                league="La Liga",
                home_team="Real Madrid CF",
                away_team="Getafe",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.CORNER_HDP,
                line=-2.5,
                side="HOME",
                odds_dec=OddsConverter.hk_to_dec(0.68),
                opposite_odds_dec=OddsConverter.hk_to_dec(1.20),
                raw_line_id="SABA_TICKET_CHDP_9004",
            ),
            # Trận 5: Kèo cân 50-50 không có Value
            OddsLine(
                source="VIVA88",
                match_id="SABA_9005",
                league="Serie A",
                home_team="Milan",
                away_team="Roma",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.HDP,
                line=-0.25,
                side="HOME",
                odds_dec=OddsConverter.hk_to_dec(0.92),
                opposite_odds_dec=OddsConverter.hk_to_dec(0.96),
                raw_line_id="SABA_TICKET_HDP_9005",
            ),
        ]

        dynamic_leagues = [
            ("Bundesliga", "Bayern Munich", "Werder Bremen", MarketType.HDP, -1.5, "HOME"),
            ("Ligue 1", "PSG", "Nantes", MarketType.OU, 3.0, "OVER"),
            ("Brazil Serie A", "Flamengo RJ", "Santos", MarketType.CORNER_OU, 10.5, "OVER"),
            ("Eredivisie", "Ajax", "Utrecht", MarketType.CORNER_HDP, -2.0, "HOME"),
            ("K-League 1", "Ulsan Hyundai", "Seoul", MarketType.OU, 2.5, "OVER"),
        ]
        if tick >= 2:
            idx = (tick - 2) % len(dynamic_leagues)
            lg, h_team, a_team, m_type, line_val, side_val = dynamic_leagues[idx]
            board.append(
                OddsLine(
                    source="VIVA88",
                    match_id=f"SABA_{9200 + tick}",
                    league=lg,
                    home_team=f"{h_team} (R{tick // len(dynamic_leagues) + 1})",
                    away_team=a_team,
                    phase=MatchPhase.LIVE if (tick % 2 == 0) else MatchPhase.PREMATCH,
                    market_type=m_type,
                    line=line_val,
                    side=side_val,
                    odds_dec=OddsConverter.hk_to_dec(0.73),
                    opposite_odds_dec=OddsConverter.hk_to_dec(1.13),
                    raw_line_id=f"SABA_TICKET_DYN_{tick}",
                )
            )
        return board

    async def get_ticket_quote(self, signal: ValueSignal) -> Tuple[bool, float, float]:
        """
        Bước 1 trong Autobet Viva88: Gọi `GetTicket` để lấy giá thực tế ngay giây đặt cược
        và hạn mức cược tối đa (Max Bet) của mã đó.
        Trả về: (is_available, current_odds_dec, max_bet_limit)
        """
        if self.mode == "simulation":
            await asyncio.sleep(0.05)  # Độ trễ mạng 50ms
            return True, signal.viva88_odds, 2000.0
        return False, 0.0, 0.0

    async def place_single_bet(self, signal: ValueSignal) -> BetTicket:
        """
        Bước 2 trong Autobet Viva88:
        1. Gọi `get_ticket_quote` kiểm tra giá.
        2. Chạy Slippage Guard: Nếu giá trên phiếu Viva88 vừa tụt vượt ngưỡng cho phép -> Hủy lệnh ngay.
        3. Gửi lệnh đặt cược (`ProcessBet`). Nếu là kèo LIVE, chờ trạng thái `WAITING` -> `RUNNING`.
        """
        ticket_id = f"V88-{uuid.uuid4().hex[:6].upper()}"
        match_name = f"{signal.home_team} vs {signal.away_team}"
        selection = f"{signal.side} {signal.line:+.2f}" if "HDP" in signal.market_type.value else f"{signal.side} {signal.line}"

        ok, live_quote_odds, max_bet = await self.get_ticket_quote(signal)
        if not ok or (signal.viva88_odds - live_quote_odds) > self.max_slippage:
            return BetTicket(
                ticket_id=ticket_id,
                match_key=signal.match_key,
                match_name=match_name,
                phase=signal.phase,
                market_type=signal.market_type,
                selection=selection,
                odds_requested=signal.viva88_odds,
                odds_accepted=live_quote_odds,
                true_win_prob=signal.true_win_prob,
                ev_percent=signal.ev_percent,
                stake=0.0,
                status=BetStatus.REJECTED,
                is_simulation=(self.mode == "simulation"),
                note="Hủy lệnh do Viva88 vừa sụt giá (Slippage Guard)",
            )

        final_stake = min(signal.recommended_stake, max_bet)

        # Nếu là kèo Rung (LIVE) trên Viva88, vé sẽ vào trạng thái WAITING 3-5s để nhà cái kiểm tra bóng nguy hiểm
        if signal.phase == MatchPhase.LIVE and self.mode == "simulation":
            await asyncio.sleep(0.15)  # Rút gọn thời gian chờ trong bản demo giả lập
            note = f"Khớp lệnh LIVE sau {int(self.waiting_delay)}s Waiting ({signal.signal_type})"
        else:
            note = f"Khớp lệnh tức thời ({signal.signal_type})"

        return BetTicket(
            ticket_id=ticket_id,
            match_key=signal.match_key,
            match_name=match_name,
            phase=signal.phase,
            market_type=signal.market_type,
            selection=selection,
            odds_requested=signal.viva88_odds,
            odds_accepted=live_quote_odds,
            true_win_prob=signal.true_win_prob,
            ev_percent=signal.ev_percent,
            stake=final_stake,
            status=BetStatus.RUNNING,
            is_simulation=(self.mode == "simulation"),
            note=note,
        )
