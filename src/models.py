from dataclasses import dataclass, field
from enum import Enum
import time


class MarketType(str, Enum):
    HDP = "HDP"                 # Asian Handicap (Chấp bàn thắng)
    OU = "OU"                   # Over/Under Goals (Tài Xỉu bàn thắng)
    CORNER_OU = "CORNER_OU"     # Over/Under Corners (Tài Xỉu góc)
    CORNER_HDP = "CORNER_HDP"   # Handicap Corners (Chấp góc)


class MatchPhase(str, Enum):
    PREMATCH = "PREMATCH"
    LIVE = "LIVE"


class BetStatus(str, Enum):
    WAITING = "WAITING"         # Đang chờ nhà cái Saba duyệt vé (kèo Rung)
    RUNNING = "RUNNING"         # Vé đang chạy - Chờ kết quả trận đấu thực tế
    REJECTED = "REJECTED"       # Từ chối do trượt giá (Slippage) hoặc bóng nguy hiểm
    WON = "WON"                 # Thắng đủ
    HALF_WON = "HALF_WON"       # Thắng nửa tiền (kèo .25, .75)
    PUSH = "PUSH"               # Hòa tiền (Hoàn cược)
    HALF_LOST = "HALF_LOST"     # Thua nửa tiền (kèo .25, .75)
    LOST = "LOST"               # Thua đủ


@dataclass
class OddsLine:
    source: str                 # "PINNACLE" hoặc "VIVA88"
    match_id: str
    saba_id: str
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
    corners_home: int = 0
    corners_away: int = 0
    minute: str = "Pre"
    status_code: int = 0        # Viva88 Status: 0=Pre, 10=1H, 20=HT, 30=2H, 50=FT
    timestamp: float = field(default_factory=time.time)
    raw_line_id: str = ""


@dataclass
class ValueSignal:
    match_id: str
    saba_id: str
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
    sharp_odds: float           # Odds chuẩn từ Sharp/Model
    fair_odds: float            # Odds thực sau khi khử phế
    true_win_prob: float        # Xác suất thắng thực tế (0.0 -> 1.0)
    ev_percent: float           # % Lợi nhuận kỳ vọng (+EV)
    steam_drop: float           # Mức sụt giá tại Sharp (nếu có Steam)
    signal_type: str            # "VALUE_+EV" hoặc "STEAM_MOVE"
    recommended_stake: float
    score_home: int = 0
    score_away: int = 0
    corners_home: int = 0
    corners_away: int = 0
    minute: str = "Pre"
    raw_line_id: str = ""


@dataclass
class BetTicket:
    ticket_id: str
    match_id: str
    saba_id: str
    match_key: str
    league: str
    match_name: str
    phase: MatchPhase
    market_type: MarketType
    line: float
    side: str
    selection: str              # VD: "OVER 2.5" hoặc "HOME -0.5"
    odds_requested: float
    odds_accepted: float
    true_win_prob: float
    ev_percent: float
    stake: float
    status: BetStatus
    score_at_bet: str = "0-0"   # Tỉ số lúc vào kèo (quan trọng cho kèo Chấp Live Saba)
    current_score: str = "0-0"
    current_corners: str = "0-0"
    current_minute: str = "Pre"
    pnl: float = 0.0            # Lãi/Lỗ thực tế sau khi trận đấu kết thúc
    is_simulation: bool = True
    created_at: float = field(default_factory=time.time)
    note: str = ""
