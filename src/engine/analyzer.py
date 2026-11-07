from collections import defaultdict, deque
import math
import time
from typing import Deque, Dict, Optional, Tuple
from src.models import OddsLine, ValueSignal


class OddsConverter:
    """Chuyển đổi giữa các định dạng Odds của Viva88 (HK, Malay, Decimal)."""

    @staticmethod
    def hk_to_dec(hk_odds: float) -> float:
        return round(hk_odds + 1.0, 4)

    @staticmethod
    def dec_to_hk(dec_odds: float) -> float:
        return round(dec_odds - 1.0, 4)

    @staticmethod
    def malay_to_dec(my_odds: float) -> float:
        if my_odds > 0:
            return round(my_odds + 1.0, 4)
        elif my_odds < 0:
            return round(1.0 - (1.0 / my_odds), 4)
        return 1.0


class SignalAnalyzer:
    """
    Bộ não tính toán:
    1. Khử phế nhà cái Sharp (Power / Multiplicative De-vig) -> Xác suất thắng thực (True Win Rate).
    2. Theo dõi lịch sử biến động giá (Steam Detector).
    3. Tính lợi thế +EV và khối lượng vào tiền theo Fractional Kelly.
    """

    def __init__(self, config: dict):
        self.cfg = config["strategy"]
        self.bankroll_cfg = config["bankroll"]
        # Lưu lịch sử biến động giá Sharp: key -> deque[(timestamp, odds_dec)]
        self._sharp_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=30)
        )

    @staticmethod
    def devig_power(odds_target: float, odds_opposite: float) -> Tuple[float, float]:
        """
        Khử biên lợi nhuận nhà cái (Power Method - chuẩn xác hơn chia tuyến tính).
        Giải tìm mũ k sao cho: (1/odds_target)^k + (1/odds_opposite)^k = 1.
        Trả về: (true_win_probability, fair_decimal_odds)
        """
        if odds_target <= 1.01 or odds_opposite <= 1.01:
            return 0.0, 99.0

        imp_a = 1.0 / odds_target
        imp_b = 1.0 / odds_opposite
        overround = imp_a + imp_b
        if overround <= 1.0:
            return imp_a, odds_target

        # Lặp Newton-Raphson tìm hệ số mũ k của Power Devig
        k = math.log(2.0) / math.log(2.0 / overround)
        for _ in range(5):
            f = (imp_a ** k) + (imp_b ** k) - 1.0
            df = (imp_a ** k) * math.log(imp_a) + (imp_b ** k) * math.log(imp_b)
            if abs(df) < 1e-9:
                break
            k -= f / df

        true_prob = imp_a ** k
        fair_odds = 1.0 / true_prob if true_prob > 0 else 99.0
        return round(true_prob, 4), round(fair_odds, 4)

    def record_sharp_tick(self, line_key: str, odds_dec: float) -> float:
        """Ghi nhận giá Sharp và trả về mức sụt giá (Steam Drop) trong cửa sổ thời gian."""
        now = time.time()
        window = self.cfg.get("steam_window_sec", 120)
        hist = self._sharp_history[line_key]
        hist.append((now, odds_dec))

        # Xóa các điểm cũ quá cửa sổ
        while hist and (now - hist[0][0]) > window:
            hist.popleft()

        if len(hist) < 2:
            return 0.0

        max_recent_odds = max(item[1] for item in hist)
        drop = round(max_recent_odds - odds_dec, 4)
        return max(0.0, drop)

    def calculate_kelly_stake(self, true_prob: float, dec_odds: float, current_bankroll: float) -> float:
        """Tính tiền cược theo công thức Fractional Kelly + làm tròn chống phát hiện."""
        b = dec_odds - 1.0
        if b <= 0:
            return 0.0
        q = 1.0 - true_prob
        full_kelly = (b * true_prob - q) / b
        if full_kelly <= 0:
            return 0.0

        fraction = self.bankroll_cfg.get("kelly_fraction", 0.25)
        max_pct = self.bankroll_cfg.get("max_stake_percent", 3.0) / 100.0
        min_stake = self.bankroll_cfg.get("min_stake", 10.0)
        round_step = self.bankroll_cfg.get("round_step", 5)

        stake_pct = min(full_kelly * fraction, max_pct)
        raw_stake = current_bankroll * stake_pct

        # Làm tròn theo bội số (VD: 5, 10) để giống người chơi thật trên Viva88
        rounded_stake = max(min_stake, round(raw_stake / round_step) * round_step)
        return float(rounded_stake)

    def evaluate_pair(
        self,
        sharp_line: OddsLine,
        viva_line: OddsLine,
        match_key: str,
        current_bankroll: float,
    ) -> Optional[ValueSignal]:
        """So sánh 1 cặp kèo (Sharp vs Viva88) để quyết định có nổ lệnh 1 cửa hay không."""
        true_prob, fair_odds = self.devig_power(
            sharp_line.odds_dec, sharp_line.opposite_odds_dec
        )

        # 1. Kiểm tra bộ lọc Tỉ lệ thắng thực (Win Rate cao)
        min_prob = self.cfg.get("min_win_prob", 0.56)
        max_prob = self.cfg.get("max_win_prob", 0.75)
        if not (min_prob <= true_prob <= max_prob):
            return None

        # 2. Tính lợi nhuận kỳ vọng (+EV%) trên giá của Viva88
        ev = (true_prob * viva_line.odds_dec) - 1.0
        ev_percent = round(ev * 100.0, 2)

        # 3. Kiểm tra biến động dòng tiền (Steam Move)
        line_key = f"{match_key}:{sharp_line.market_type.value}:{sharp_line.line}:{sharp_line.side}"
        steam_drop = self.record_sharp_tick(line_key, sharp_line.odds_dec)
        is_steam = steam_drop >= self.cfg.get("steam_drop_ticks", 0.08)

        min_ev = self.cfg.get("min_ev_percent", 3.0)
        # Nếu có Steam mạnh thì cho phép ngưỡng EV thấp hơn 1 chút (1.5%), ngược lại phải >= min_ev
        effective_min_ev = 1.5 if is_steam else min_ev
        if ev_percent < effective_min_ev:
            return None

        stake = self.calculate_kelly_stake(true_prob, viva_line.odds_dec, current_bankroll)
        if stake <= 0:
            return None

        signal_type = "STEAM_MOVE" if is_steam else "VALUE_+EV"
        return ValueSignal(
            match_key=match_key,
            league=viva_line.league,
            home_team=viva_line.home_team,
            away_team=viva_line.away_team,
            phase=viva_line.phase,
            market_type=viva_line.market_type,
            line=viva_line.line,
            side=viva_line.side,
            viva88_odds=viva_line.odds_dec,
            viva88_hk_odds=OddsConverter.dec_to_hk(viva_line.odds_dec),
            sharp_odds=sharp_line.odds_dec,
            fair_odds=fair_odds,
            true_win_prob=true_prob,
            ev_percent=ev_percent,
            steam_drop=steam_drop,
            signal_type=signal_type,
            recommended_stake=stake,
            raw_line_id=viva_line.raw_line_id,
        )
