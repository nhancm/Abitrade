import random
from typing import List
import httpx
from src.models import MarketType, MatchPhase, OddsLine


class SharpFeedClient:
    """
    Bộ thu thập dữ liệu nhà cái chuẩn (Sharp Feed - Pinnacle / The-Odds-API):
    1. Hỗ trợ lấy dữ liệu miễn phí từ The-Odds-API (nếu điền key) hoặc Arcadia Pinnacle Guest API.
    2. Tích hợp bộ phát dữ liệu thị trường thực tế (Realistic Market Feed) cho chế độ chạy thử nghiệm
       ngay lập tức mà không cần tốn phí mua API.
    """

    def __init__(self, config: dict):
        self.cfg = config["sharp_feed"]
        self.api_key = self.cfg.get("the_odds_api_key", "").strip()

    async def fetch_sharp_lines(self, tick: int = 0) -> List[OddsLine]:
        if self.api_key:
            lines = await self._fetch_from_the_odds_api()
            if lines:
                return lines
        return self._generate_realistic_sharp_stream(tick)

    async def _fetch_from_the_odds_api(self) -> List[OddsLine]:
        """Lấy kèo Pinnacle thực tế qua gói Free của The-Odds-API (500 requests/tháng)."""
        url = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "eu",
            "markets": "spreads,totals",
            "bookmakers": "pinnacle",
            "oddsFormat": "decimal",
        }
        results: List[OddsLine] = []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return []
                for ev in resp.json():
                    home, away = ev["home_team"], ev["away_team"]
                    for bm in ev.get("bookmakers", []):
                        for mkt in bm.get("markets", []):
                            outcomes = mkt.get("outcomes", [])
                            if len(outcomes) != 2:
                                continue
                            o1, o2 = outcomes[0], outcomes[1]
                            m_type = MarketType.HDP if mkt["key"] == "spreads" else MarketType.OU
                            side1 = "HOME" if (m_type == MarketType.HDP and o1["name"] == home) else o1["name"].upper()
                            results.append(
                                OddsLine(
                                    source="PINNACLE",
                                    match_id=ev["id"],
                                    league=ev.get("sport_title", "EPL"),
                                    home_team=home,
                                    away_team=away,
                                    phase=MatchPhase.PREMATCH,
                                    market_type=m_type,
                                    line=float(o1.get("point", 0.0)),
                                    side=side1,
                                    odds_dec=float(o1["price"]),
                                    opposite_odds_dec=float(o2["price"]),
                                )
                            )
        except Exception:
            return []
        return results

    def _generate_realistic_sharp_stream(self, tick: int) -> List[OddsLine]:
        """
        Tạo luồng dữ liệu Sharp (Pinnacle) bao gồm đầy đủ:
        - Pre-match & Live (Rung)
        - Kèo Chấp (HDP), Tài Xỉu bàn thắng (OU), Tài Xỉu Phạt Góc (CORNER_OU), Chấp Góc (CORNER_HDP)
        - Mô phỏng cả sóng Steam Drop (sụt giá đột ngột ở tick >= 1)
        """
        steam_shift = -0.10 if tick >= 1 else 0.0
        base_matches = [
            # 1. Kèo Live Tài Xỉu bàn thắng (Đang ép sân mạnh, Pinnacle sụt giá Tài 2.25)
            OddsLine(
                source="PINNACLE",
                match_id="SHARP_101",
                league="Japan J1 League",
                home_team="Kawasaki Frontale",
                away_team="Urawa Red Diamonds",
                phase=MatchPhase.LIVE,
                market_type=MarketType.OU,
                line=2.25,
                side="OVER",
                odds_dec=round(1.62 + steam_shift, 3),   # Khi tick=1 tụt xuống 1.52 -> Winrate ~63% + Steam
                opposite_odds_dec=round(2.38 - steam_shift, 3),
                score_home=1,
                score_away=0,
                minute=58,
            ),
            # 2. Kèo Live Tài Góc (CORNER_OU) - Giải cỏ/Phạt góc nhà cái Saba rất hay trễ nhịp
            OddsLine(
                source="PINNACLE",
                match_id="SHARP_102",
                league="Australia A-League",
                home_team="Melbourne Victory",
                away_team="Sydney FC",
                phase=MatchPhase.LIVE,
                market_type=MarketType.CORNER_OU,
                line=9.5,
                side="OVER",
                odds_dec=1.57,
                opposite_odds_dec=2.46,
                score_home=0,
                score_away=1,
                minute=34,
            ),
            # 3. Kèo Pre-match Chấp Châu Á (HDP) có Winrate cao (P_true ~ 60.5%)
            OddsLine(
                source="PINNACLE",
                match_id="SHARP_103",
                league="English Premier League",
                home_team="Arsenal FC",
                away_team="Brighton",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.HDP,
                line=-1.0,
                side="HOME",
                odds_dec=1.59,
                opposite_odds_dec=2.45,
            ),
            # 4. Kèo Pre-match Chấp Góc (CORNER_HDP)
            OddsLine(
                source="PINNACLE",
                match_id="SHARP_104",
                league="Spain La Liga",
                home_team="Real Madrid",
                away_team="Getafe CF",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.CORNER_HDP,
                line=-2.5,
                side="HOME",
                odds_dec=1.55,
                opposite_odds_dec=2.50,
            ),
            # 5. Kèo Nhiễu (Không đủ Winrate/EV để kiểm tra bộ lọc từ chối chuẩn xác)
            OddsLine(
                source="PINNACLE",
                match_id="SHARP_105",
                league="Italy Serie A",
                home_team="AC Milan",
                away_team="AS Roma",
                phase=MatchPhase.PREMATCH,
                market_type=MarketType.HDP,
                line=-0.25,
                side="HOME",
                odds_dec=1.95,
                opposite_odds_dec=1.95,
            ),
        ]
        return base_matches
