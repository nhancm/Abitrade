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
        Tạo luồng dữ liệu Sharp (Pinnacle) biến động liên tục theo từng chu kỳ (tick):
        - Có sẵn các trận cơ sở + tự động sinh thêm trận mới sau mỗi vài chu kỳ để kiểm thử chạy dài hạn.
        - Mô phỏng biến động giá thực tế (jitter) và sóng Steam Drop (sụt giá đột ngột).
        """
        rng = random.Random(tick)
        steam_shift = -0.10 if (tick % 3 == 1) else 0.0
        base_matches = [
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
                odds_dec=round(1.62 + steam_shift, 3),
                opposite_odds_dec=round(2.38 - steam_shift, 3),
                score_home=1,
                score_away=0,
                minute=min(89, 55 + tick),
            ),
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
                minute=min(89, 30 + tick),
            ),
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

        # Cứ mỗi chu kỳ từ tick >= 2, sinh thêm trận Live/Pre-match mới để luồng chạy liên tục
        dynamic_leagues = [
            ("Germany Bundesliga", "Bayern Munich", "Werder Bremen", MarketType.HDP, -1.5, "HOME"),
            ("France Ligue 1", "PSG", "Nantes FC", MarketType.OU, 3.0, "OVER"),
            ("Brazil Serie A", "Flamengo", "Santos FC", MarketType.CORNER_OU, 10.5, "OVER"),
            ("Netherlands Eredivisie", "Ajax Amsterdam", "Utrecht", MarketType.CORNER_HDP, -2.0, "HOME"),
            ("Korea K-League 1", "Ulsan Hyundai", "FC Seoul", MarketType.OU, 2.5, "OVER"),
        ]
        if tick >= 2:
            idx = (tick - 2) % len(dynamic_leagues)
            lg, h_team, a_team, m_type, line_val, side_val = dynamic_leagues[idx]
            match_num = 200 + tick
            # Xen kẽ: có chu kỳ kèo đạt chuẩn +EV, có chu kỳ kèo bình thường để thấy rõ bộ lọc
            is_value_tick = (tick % 2 == 0)
            s_odds = round(rng.uniform(1.52, 1.62), 3) if is_value_tick else 1.92
            opp_odds = round(3.98 - s_odds, 3)
            base_matches.append(
                OddsLine(
                    source="PINNACLE",
                    match_id=f"SHARP_{match_num}",
                    league=lg,
                    home_team=f"{h_team} (R{tick // len(dynamic_leagues) + 1})",
                    away_team=a_team,
                    phase=MatchPhase.LIVE if (tick % 2 == 0) else MatchPhase.PREMATCH,
                    market_type=m_type,
                    line=line_val,
                    side=side_val,
                    odds_dec=s_odds,
                    opposite_odds_dec=opp_odds,
                    minute=rng.randint(15, 75),
                )
            )
        return base_matches
