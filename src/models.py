from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Optional


class MarketType(str, Enum):
    HDP = "HDP"                 # Asian Handicap (Chấp)
    OU = "OU"                   # Over/Under Goals (Tài Xỉu bàn thắng)
    CORNER_OU = "CORNER_OU"     # Over/Under Corners (Tài Xỉu góc)
    CORNER_HDP = "CORNER_HDP"   # Handicap Corners (Chấp góc)


class MatchPhase(str, Enum):
    PREMATCH = "PREMATCH"
    LIVE = "LIVE"


class BetStatus(str, Enum):
    WAITING = "WAITING"         # Đang chờ nhà cái Saba duyệt vé (kèo Rung)
    RUNNING = "RUNNING"         # Đã khớp lệnh thành công
    REJECTED = "REJECTED"       # Từ chối do trượt giá (Slippage) hoặc bóng nguy hiểm
    WON = "WON"
    LOST = "LOST"
    PUSH = "PUSH"               # Hòa tiền


@dataclass
class OddsLine:
    source: str                 # "PINNACLE" hoặc "VIVA88"
    match_id: str
    league: str
    home_team: str
    away_team: str
    phase: MatchPhase
    market_type: MarketType
    line: float                 # Mốc kèo: VD -0.5, -0.75, 2.5, 9.5 (Góc)
    side: str                   # "HOME", "AWAY", "OVER", "UNDER"
    odds_dec: float             # Giá quy chuẩn Decimal (VD: 1.95)
    opposite_odds_dec: float    # Giá cửa đối diện để khử phế (De-vig)
    score_home: int = 0
    score_away: int = 0
    minute: int = 0
    timestamp: float = field(default_factory=time.time)
    raw_line_id: str = ""       # ID dòng cược trên Viva88 dùng cho GetTicket/PlaceBet


@dataclass
class ValueSignal:
    match_key: str
    league: str
    home_team: str
    away_team: str
    phase: MatchPhase
    market_type: MarketType
    line: float
    side: str
    viva88_odds: float          # Odds đặt tại Viva88 (Decimal)
    viva88_hk_odds: float       # Odds HK tương ứng (VD: 0.95)
    sharp_odds: float           # Odds tại Pinnacle
    fair_odds: float            # Odds thực sau khi khử phế
    true_win_prob: float        # Xác suất thắng thực tế (0.0 -> 1.0)
    ev_percent: float           # % Lợi nhuận kỳ vọng (+EV)
    steam_drop: float           # Mức sụt giá tại Sharp (nếu có Steam)
    signal_type: str            # "VALUE_+EV" hoặc "STEAM_MOVE"
    recommended_stake: float
    raw_line_id: str = ""


@dataclass
class BetTicket:
    ticket_id: str
    match_key: str
    match_name: str
    phase: MatchPhase
    market_type: MarketType
    selection: str              # VD: "OVER 2.5" hoặc "HOME -0.5"
    odds_requested: float
    odds_accepted: float
    true_win_prob: float
    ev_percent: float
    stake: float
    status: BetStatus
    is_simulation: bool = True
    created_at: float = field(default_factory=time.time)
    note: str = ""
