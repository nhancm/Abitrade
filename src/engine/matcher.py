import re
from typing import Dict, List, Tuple
from rapidfuzz import fuzz
from src.models import OddsLine


class MatchMatcher:
    """
    Chuẩn hóa tên đội bóng giữa Sharp (Pinnacle) và Viva88 (Saba Sports),
    ghép đúng trận đấu, đúng loại kèo (HDP, OU, Corner) và cùng mốc line.
    """

    STOP_WORDS = re.compile(
        r"\b(fc|sc|cf|ac|afc|united|utd|city|club|deportivo|real|sporting|u19|u20|u21|u23|women|w|reserves|corners)\b",
        re.IGNORECASE,
    )

    @classmethod
    def normalize_name(cls, name: str) -> str:
        cleaned = cls.STOP_WORDS.sub("", name.lower())
        cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
        return " ".join(cleaned.split())

    @classmethod
    def is_same_match(cls, a: OddsLine, b: OddsLine, threshold: int = 70) -> bool:
        if a.phase != b.phase or a.market_type != b.market_type:
            return False
        if abs(a.line - b.line) > 1e-4 or a.side != b.side:
            return False

        home_a, away_a = cls.normalize_name(a.home_team), cls.normalize_name(a.away_team)
        home_b, away_b = cls.normalize_name(b.home_team), cls.normalize_name(b.away_team)

        score_home = fuzz.token_set_ratio(home_a, home_b)
        score_away = fuzz.token_set_ratio(away_a, away_b)
        return score_home >= threshold and score_away >= threshold

    @classmethod
    def build_match_key(cls, line: OddsLine) -> str:
        h = cls.normalize_name(line.home_team).replace(" ", "_")
        a = cls.normalize_name(line.away_team).replace(" ", "_")
        return f"{h}_vs_{a}"

    @classmethod
    def pair_lines(
        cls, sharp_lines: List[OddsLine], viva_lines: List[OddsLine]
    ) -> List[Tuple[str, OddsLine, OddsLine]]:
        """Ghép các dòng kèo tương ứng giữa Sharp và Viva88."""
        paired: List[Tuple[str, OddsLine, OddsLine]] = []
        # Index nhanh theo (phase, market_type, line, side) để giảm độ phức tạp O(N*M)
        viva_buckets: Dict[Tuple, List[OddsLine]] = {}
        for v in viva_lines:
            key = (v.phase, v.market_type, round(v.line, 2), v.side)
            viva_buckets.setdefault(key, []).append(v)

        for s in sharp_lines:
            key = (s.phase, s.market_type, round(s.line, 2), s.side)
            candidates = viva_buckets.get(key, [])
            for v in candidates:
                if cls.is_same_match(s, v):
                    match_key = cls.build_match_key(v)
                    paired.append((match_key, s, v))
                    break
        return paired
