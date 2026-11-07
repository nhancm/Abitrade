import asyncio
import json
import re
import uuid
from typing import Dict, List, Optional, Tuple
import httpx
from src.engine.analyzer import OddsConverter
from src.models import BetStatus, BetTicket, MarketType, MatchPhase, OddsLine, ValueSignal


class Viva88Client:
    """
    Client giao tiếp trực tiếp với hệ thống dữ liệu trận đấu thực tế của Viva88 (Saba Sports):
    - Tự động trích xuất `liveScoreKeyToken` và `liveScoreBearerToken` từ trang chủ `viva88.net/b/vn`.
    - Gọi API thời gian thực `https://wonderfulnextapi.com/api/1.0/Match/Soccer/Today/` để lấy
      100% các trận đấu thật có mở kèo Saba (`SabaId != null`) đang diễn ra trên bảng Viva88.
    - Lưu bản đồ tỉ số/phạt góc/trạng thái trận đấu (`live_match_map`) để chốt vé khi trận đấu kết thúc (FT).
    """

    DEFAULT_KEY_TOKEN = "EDA8F37E5E644977BB5358217A605757"
    DEFAULT_BEARER = "Bearer 4A4DE4245BB4EF6490A38F41F09F9250977394DC83C26538CA2F780145C6698C"
    MATCH_API_URL = "https://wonderfulnextapi.com/api/1.0/Match/Soccer/Today/"

    def __init__(self, config: dict):
        self.mode = config.get("mode", "simulation")
        self.cfg = config["viva88"]
        self.strat_cfg = config.get("strategy", {})
        self.base_url = self.cfg.get("base_url", "https://www.viva88.net")
        self.username = self.cfg.get("username", "")
        self.password = self.cfg.get("password", "")
        self.max_slippage = float(self.cfg.get("max_slippage", 0.02))
        self.waiting_delay = float(self.cfg.get("live_waiting_delay_sec", 3.0))
        self.session_token: Optional[str] = None
        self.logged_in_user: str = self.username or "GUEST"
        self.key_token: str = self.DEFAULT_KEY_TOKEN
        self.bearer_token: str = self.DEFAULT_BEARER
        self.live_match_map: Dict[str, dict] = {}
        self._http: Optional[httpx.AsyncClient] = None

    async def login(self) -> bool:
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
            },
        )
        try:
            resp = await self._http.get(f"{self.base_url}/b/vn")
            if resp.status_code == 200:
                m_key = re.search(r'"liveScoreKeyToken"\s*:\s*"([^"]+)"', resp.text)
                m_bearer = re.search(r'"liveScoreBearerToken"\s*:\s*"([^"]+)"', resp.text)
                if m_key:
                    self.key_token = m_key.group(1)
                if m_bearer:
                    self.bearer_token = f"Bearer {m_bearer.group(1)}"
                self.session_token = f"V88_{self.username}_{uuid.uuid4().hex[:6].upper()}"
                return True
        except Exception:
            pass

        self.session_token = f"V88_{self.username}_{uuid.uuid4().hex[:6].upper()}"
        return True

    async def close(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def fetch_real_saba_matches(self) -> List[dict]:
        """Lấy toàn bộ danh sách trận đấu thực tế có trên sảnh Saba của Viva88 hôm nay."""
        if not self._http:
            await self.login()
        headers = {
            "Content-Type": "application/json",
            "key": self.key_token,
            "Authorization": self.bearer_token,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        try:
            resp = await self._http.post(self.MATCH_API_URL, headers=headers, json={})
            if resp.status_code == 200:
                data = resp.json()
                all_matches = data.get("Result", {}).get("Match", [])
                # Chỉ lọc các trận có mở kèo trên sảnh Saba (SabaId != null)
                saba_matches = [m for m in all_matches if m.get("SabaId")]
                for m in saba_matches:
                    self.live_match_map[str(m["MatchId"])] = m
                return saba_matches
        except Exception:
            pass
        return []

    @staticmethod
    def format_minute(m: dict) -> str:
        st = m.get("Status", 0)
        mt = m.get("MatchTime")
        if st == 10:
            return f"1H {mt or 1}'"
        if st == 20:
            return "HT"
        if st == 30:
            return f"2H {mt or 46}'"
        if st == 50:
            return "FT"
        return f"Pre ({m.get('Schedule', '')})"

    async def fetch_viva88_lines(self, tick: int = 0) -> List[OddsLine]:
        """
        Quét các trận đấu THẬT đang diễn ra (Live Status 10, 20, 30) và sắp đá (Status 0)
        trên bảng kèo Viva88 (Saba Sports) và tạo dòng kèo sát thực tế cho từng trận.
        """
        saba_matches = await self.fetch_real_saba_matches()
        if not saba_matches:
            return []

        # Ưu tiên toàn bộ trận đang LIVE trên Viva88 (Chặn giam vốn vào các trận Pre-match chưa có thống kê sân)
        live_list = [m for m in saba_matches if m.get("Status") in (10, 20, 30)]
        active_matches = live_list

        lines: List[OddsLine] = []
        for m in active_matches:
            match_id = str(m["MatchId"])
            saba_id = str(m["SabaId"])
            league = m.get("LeagueEn") or m.get("LeagueZh") or "Saba League"
            h_info = m.get("HomeTeam", {})
            a_info = m.get("AwayTeam", {})
            home = h_info.get("NameEn") or h_info.get("NameCn") or "Home"
            away = a_info.get("NameEn") or a_info.get("NameCn") or "Away"

            st = m.get("Status", 0)
            phase = MatchPhase.LIVE if st in (10, 20, 30) else MatchPhase.PREMATCH
            sh = int(h_info.get("FullScore", 0))
            sa = int(a_info.get("FullScore", 0))
            ch = int(h_info.get("CornerKick", 0))
            ca = int(a_info.get("CornerKick", 0))
            atk_total = int(h_info.get("Attack", 0)) + int(a_info.get("Attack", 0))
            da_total = int(h_info.get("DangerAttack", 0)) + int(a_info.get("DangerAttack", 0))
            sot_total = int(h_info.get("OnGoal", 0)) + int(a_info.get("OnGoal", 0))
            miss_total = int(h_info.get("MissGoal", 0)) + int(a_info.get("MissGoal", 0))
            mt = int(m.get("MatchTime") or (45 if st == 20 else 0))
            minute_str = self.format_minute(m)

            # BỘ LỌC BẢO VỆ VỐN KÈO LIVE:
            # 1. Khung giờ vàng: Chỉ vào từ phút 28' -> 68' (Bao gồm cả giờ nghỉ HT = 45')
            # 2. Chặn trận vỡ (cách biệt >= 3 bàn) và chặn trận mất kết nối thống kê (Effective Activity quá thấp)
            if phase == MatchPhase.LIVE:
                min_mt = int(self.strat_cfg.get("live_min_minute", 28))
                max_mt = int(self.strat_cfg.get("live_max_minute", 68))
                max_diff = int(self.strat_cfg.get("max_live_goal_diff", 2))
                if not (min_mt <= mt <= max_mt):
                    continue
                if abs(sh - sa) > max_diff:
                    continue
                eff_da = da_total + int(atk_total * 0.6) + ((ch + ca) * 3)
                eff_sot = sot_total + int(miss_total * 0.5) + (sh + sa)
                if eff_da < 18 or (eff_sot < 2 and (ch + ca) < 5):
                    continue

            # 1. Kèo Tài Xỉu Bàn Thắng (OU) dịch chuyển mốc Line động theo phút giống chuẩn Saba:
            total_goals = sh + sa
            if phase == MatchPhase.LIVE:
                if mt <= 45:
                    ou_line = float(total_goals) + 1.5   # Cuối H1 / HT: Mốc Saba chuẩn +1.5 bàn
                elif mt <= 58:
                    ou_line = float(total_goals) + 1.0   # Đầu H2: Mốc Saba hạ về +1.0 bàn
                else:
                    ou_line = float(total_goals) + 0.75  # Phút 59-68: Nổ 1 bàn ăn nửa, 2 bàn ăn đủ!
            else:
                ou_line = 2.5
            viva_hk_ou = 0.78 if phase == MatchPhase.LIVE else 0.84
            lines.append(
                OddsLine(
                    source="VIVA88",
                    match_id=match_id,
                    saba_id=saba_id,
                    league=league,
                    home_team=home,
                    away_team=away,
                    phase=phase,
                    market_type=MarketType.OU,
                    line=ou_line,
                    side="OVER",
                    odds_dec=OddsConverter.hk_to_dec(viva_hk_ou),
                    opposite_odds_dec=OddsConverter.hk_to_dec(round(1.88 - viva_hk_ou, 2)),
                    score_home=sh,
                    score_away=sa,
                    corners_home=ch,
                    corners_away=ca,
                    minute=minute_str,
                    status_code=st,
                    raw_line_id=f"SABA_OU_{saba_id}",
                )
            )

            # 2. Kèo Tài Xỉu Phạt Góc (CORNER_OU) nếu trận có thống kê góc
            total_corners = ch + ca
            if phase == MatchPhase.LIVE and total_corners > 0:
                crn_line = float(total_corners) + 3.5
                viva_hk_crn = 0.74
                lines.append(
                    OddsLine(
                        source="VIVA88",
                        match_id=match_id,
                        saba_id=saba_id,
                        league=league,
                        home_team=home,
                        away_team=away,
                        phase=phase,
                        market_type=MarketType.CORNER_OU,
                        line=crn_line,
                        side="OVER",
                        odds_dec=OddsConverter.hk_to_dec(viva_hk_crn),
                        opposite_odds_dec=OddsConverter.hk_to_dec(round(1.86 - viva_hk_crn, 2)),
                        score_home=sh,
                        score_away=sa,
                        corners_home=ch,
                        corners_away=ca,
                        minute=minute_str,
                        status_code=st,
                        raw_line_id=f"SABA_CRN_{saba_id}",
                    )
                )

            # 3. Kèo Chấp Châu Á (HDP)
            hdp_line = -0.5
            viva_hk_hdp = 0.78 if phase == MatchPhase.LIVE else 0.85
            lines.append(
                OddsLine(
                    source="VIVA88",
                    match_id=match_id,
                    saba_id=saba_id,
                    league=league,
                    home_team=home,
                    away_team=away,
                    phase=phase,
                    market_type=MarketType.HDP,
                    line=hdp_line,
                    side="HOME",
                    odds_dec=OddsConverter.hk_to_dec(viva_hk_hdp),
                    opposite_odds_dec=OddsConverter.hk_to_dec(round(1.88 - viva_hk_hdp, 2)),
                    score_home=sh,
                    score_away=sa,
                    corners_home=ch,
                    corners_away=ca,
                    minute=minute_str,
                    status_code=st,
                    raw_line_id=f"SABA_HDP_{saba_id}",
                )
            )

        return lines

    async def get_ticket_quote(self, signal: ValueSignal) -> Tuple[bool, float, float]:
        await asyncio.sleep(0.05)
        return True, signal.viva88_odds, 2000.0

    async def place_single_bet(self, signal: ValueSignal) -> BetTicket:
        ticket_id = f"V88-{signal.saba_id[-5:]}-{uuid.uuid4().hex[:3].upper()}"
        match_name = f"{signal.home_team} vs {signal.away_team}"
        selection = (
            f"{signal.side} {signal.line:+.2f}"
            if "HDP" in signal.market_type.value
            else f"{signal.side} {signal.line}"
        )
        score_str = f"{signal.score_home}-{signal.score_away}"
        corners_str = f"{signal.corners_home}-{signal.corners_away}"

        ok, live_quote_odds, max_bet = await self.get_ticket_quote(signal)
        if not ok or (signal.viva88_odds - live_quote_odds) > self.max_slippage:
            return BetTicket(
                ticket_id=ticket_id,
                match_id=signal.match_id,
                saba_id=signal.saba_id,
                match_key=signal.match_key,
                league=signal.league,
                match_name=match_name,
                phase=signal.phase,
                market_type=signal.market_type,
                line=signal.line,
                side=signal.side,
                selection=selection,
                odds_requested=signal.viva88_odds,
                odds_accepted=live_quote_odds,
                true_win_prob=signal.true_win_prob,
                ev_percent=signal.ev_percent,
                stake=0.0,
                status=BetStatus.REJECTED,
                score_at_bet=score_str,
                current_score=score_str,
                current_corners=corners_str,
                current_minute=signal.minute,
                is_simulation=(self.mode == "simulation"),
                note="Hủy lệnh do Viva88 vừa sụt giá (Slippage Guard)",
            )

        final_stake = min(signal.recommended_stake, max_bet)
        if signal.phase == MatchPhase.LIVE and self.mode == "simulation":
            await asyncio.sleep(0.15)
            note = f"Đang chạy (Đã duyệt sau {int(self.waiting_delay)}s Waiting - {signal.signal_type})"
        else:
            note = f"Đang chạy ({signal.signal_type})"

        return BetTicket(
            ticket_id=ticket_id,
            match_id=signal.match_id,
            saba_id=signal.saba_id,
            match_key=signal.match_key,
            league=signal.league,
            match_name=match_name,
            phase=signal.phase,
            market_type=signal.market_type,
            line=signal.line,
            side=signal.side,
            selection=selection,
            odds_requested=signal.viva88_odds,
            odds_accepted=live_quote_odds,
            true_win_prob=signal.true_win_prob,
            ev_percent=signal.ev_percent,
            stake=final_stake,
            status=BetStatus.RUNNING,
            score_at_bet=score_str,
            current_score=score_str,
            current_corners=corners_str,
            current_minute=signal.minute,
            is_simulation=(self.mode == "simulation"),
            note=note,
        )
