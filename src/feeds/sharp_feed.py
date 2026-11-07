import math
from typing import Dict, List
import httpx
from src.models import MarketType, MatchPhase, OddsLine


class SharpFeedClient:
    """
    Bộ định giá chuẩn (Sharp Reference & Live xG/Momentum Model):
    - Phân tích trực tiếp các thông số thời gian thực của từng trận đấu trên Viva88/Saba:
      + `DangerAttack` (Tấn công nguy hiểm), `OnGoal` (Sút trúng đích), `MissGoal`, `CornerKick`, `MatchTime`.
    - Kết hợp công thức Poisson động (Dynamic In-Play Poisson) để tính Xác suất thực (Fair Odds)
      như cách nhà cái Sharp (Pinnacle) định giá, giúp phát hiện những trận đang ép sân dữ dội
      mà bảng giá Saba chưa kịp giảm sâu.
    """

    def __init__(self, config: dict):
        self.cfg = config["sharp_feed"]

    def build_sharp_lines_from_real_matches(
        self, saba_matches: List[dict], viva_lines: List[OddsLine], tick: int = 0
    ) -> List[OddsLine]:
        match_by_id: Dict[str, dict] = {str(m["MatchId"]): m for m in saba_matches}
        sharp_lines: List[OddsLine] = []

        for v in viva_lines:
            m = match_by_id.get(v.match_id)
            if not m:
                continue

            h = m.get("HomeTeam", {})
            a = m.get("AwayTeam", {})
            st = m.get("Status", 0)
            mt = int(m.get("MatchTime") or (45 if st == 20 else 0))

            # Lấy thông số áp lực thực tế trên sân (Live Momentum)
            da_h = int(h.get("DangerAttack", 0))
            da_a = int(a.get("DangerAttack", 0))
            sot_h = int(h.get("OnGoal", 0))
            sot_a = int(a.get("OnGoal", 0))
            crn_h = int(h.get("CornerKick", 0))
            crn_a = int(a.get("CornerKick", 0))

            total_da = da_h + da_a
            total_sot = sot_h + sot_a
            elapsed = max(25, mt) if v.phase == MatchPhase.LIVE else 0

            if v.market_type == MarketType.OU:
                if v.phase == MatchPhase.LIVE:
                    # Vì mốc kèo Saba (ou_line) đã tự động hạ từ +1.5 (H1) xuống +1.0 / +0.75 (H2) theo phút,
                    # ta tính trực tiếp áp lực tấn công mỗi phút (không phạt kép hệ số thời gian còn lại)
                    da_per_min = total_da / elapsed
                    sot_per_min = total_sot / elapsed
                    pressure_score = (da_per_min * 0.70) + (sot_per_min * 2.60)
                    true_prob = min(0.66, max(0.45, 0.45 + (pressure_score - 0.50) * 0.30))
                else:
                    true_prob = 0.50

            elif v.market_type == MarketType.CORNER_OU:
                crn_per_min = (crn_h + crn_a) / max(25, elapsed)
                da_per_min = total_da / max(25, elapsed)
                corner_pressure = (crn_per_min * 3.8) + (da_per_min * 0.45)
                true_prob = min(0.65, max(0.46, 0.46 + (corner_pressure - 0.60) * 0.24))

            elif v.market_type == MarketType.HDP:
                if v.phase == MatchPhase.LIVE:
                    # Rút kinh nghiệm từ Log VPS:
                    # 1. CẤM đánh HDP HOME -0.5 khi Chủ nhà ĐÃ DẪN BÀN (score_diff >= 1) vì Chủ nhà sẽ lùi sâu bảo toàn tỉ số 1-0 (thua kèo Rung!)
                    # 2. CẤM đánh HDP HOME -0.5 khi Chủ nhà đang bị dẫn (score_diff < 0) để nhường suất 1-trận-1-vé cho kèo Tài Bàn Thắng (LIVE OU OVER)!
                    # 3. Chỉ đánh HDP HOME -0.5 khi tỉ số ĐANG HÒA (0-0 hoặc 1-1), Chủ nhà không bị thẻ đỏ, và áp đảo toàn diện (SOT hơn >= 3, Góc hơn >= 2)
                    score_diff = v.score_home - v.score_away
                    red_h = int(h.get("RedCard", 0))
                    if score_diff != 0 or red_h > 0 or (sot_h - sot_a) < 3 or (crn_h - crn_a) < 2:
                        true_prob = 0.45
                    else:
                        dom_da = (da_h - da_a) / max(25, elapsed)
                        dom_sot = (sot_h - sot_a) / max(25, elapsed)
                        dom_score = (dom_da * 0.75 + dom_sot * 2.2)
                        true_prob = min(0.65, max(0.45, 0.48 + dom_score * 0.30))
                else:
                    true_prob = 0.50
            else:
                true_prob = 0.50

            # Quy đổi từ True Probability sang cặp giá Pinnacle có biên lợi nhuận mỏng (2.5% margin)
            overround = 1.025
            imp_target = true_prob * overround
            imp_opp = (1.0 - true_prob) * overround
            sharp_dec = round(1.0 / max(0.05, imp_target), 3)
            opp_dec = round(1.0 / max(0.05, imp_opp), 3)

            sharp_lines.append(
                OddsLine(
                    source="PINNACLE",
                    match_id=v.match_id,
                    saba_id=v.saba_id,
                    league=v.league,
                    home_team=v.home_team,
                    away_team=v.away_team,
                    phase=v.phase,
                    market_type=v.market_type,
                    line=v.line,
                    side=v.side,
                    odds_dec=sharp_dec,
                    opposite_odds_dec=opp_dec,
                    score_home=v.score_home,
                    score_away=v.score_away,
                    corners_home=v.corners_home,
                    corners_away=v.corners_away,
                    minute=v.minute,
                    status_code=v.status_code,
                )
            )

        return sharp_lines
