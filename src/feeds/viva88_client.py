import asyncio
from datetime import datetime, timedelta
import json
import re
import time
import uuid
from typing import Dict, List, Optional, Tuple
import httpx
from src.engine.analyzer import OddsConverter
from src.models import BetStatus, BetTicket, MarketType, MatchPhase, OddsLine, ValueSignal


class Viva88Client:
    """
    Client giao tiếp trực tiếp với hệ thống dữ liệu & đặt cược thực tế của Viva88 / Bong88 (Saba Sports):
    - Hỗ trợ tự động chuyển đổi tên miền phổ biến tại VN (`bong88.com` -> `viva88.net` -> `viva88.com`).
    - Hỗ trợ gắn Proxy Hồ Chí Minh độc lập cho từng tài khoản (`ip:port:user:pass` hoặc `http/socks5`),
      đo độ trễ thực tế (`last_ping_ms`), kiểm tra IP đầu ra (`proxy_exit_ip`) và cơ chế Kill-Switch chống lộ IP.
    - Tự động phát hiện mức cược tối thiểu (`detected_min_stake`) và số dư thực tế của sảnh Saba.
    """

    DEFAULT_KEY_TOKEN = "EDA8F37E5E644977BB5358217A605757"
    DEFAULT_BEARER = "Bearer 4A4DE4245BB4EF6490A38F41F09F9250977394DC83C26538CA2F780145C6698C"
    MATCH_API_URL = "https://wonderfulnextapi.com/api/1.0/Match/Soccer/Today/"
    MATCH_DATE_API_URL = "https://wonderfulnextapi.com/api/1.0/Match/Soccer/Date/"

    def __init__(self, config: dict, account_override: Optional[dict] = None):
        self.mode = config.get("mode", "simulation")
        self.cfg = dict(config.get("viva88", {}))
        if account_override:
            self.cfg.update(account_override)
        self.strat_cfg = config.get("strategy", {})
        self.bank_cfg = config.get("bankroll", {})
        self.multi_cfg = config.get("multi_account", {})
        self.base_url = self.cfg.get("domain") or self.cfg.get("base_url", "https://www.bong88.com")
        fallback_list = self.cfg.get("fallback_urls", ["https://www.bong88.com", "https://www.viva88.net", "https://www.viva88.com"])
        self.candidate_domains = [self.base_url] + [d for d in fallback_list if d != self.base_url]
        self.username = self.cfg.get("username", "")
        self.password = self.cfg.get("password", "")
        self.raw_proxy = str(self.cfg.get("proxy", "") or "").strip()
        self.proxy_url: Optional[str] = self.normalize_proxy(self.raw_proxy)
        self.proxy_exit_ip: str = "Direct (Local/VPS)"
        self.last_ping_ms: float = 0.0
        self.proxy_healthy: bool = True
        self.detected_min_stake: float = float(self.bank_cfg.get("min_stake", 10.0))
        self.detected_balance: Optional[float] = None
        self.max_slippage = float(self.cfg.get("max_slippage", 0.02))
        self.waiting_delay = float(self.cfg.get("live_waiting_delay_sec", 3.0))
        self.session_token: Optional[str] = None
        self.logged_in_user: str = self.username or "GUEST"
        self.key_token: str = self.DEFAULT_KEY_TOKEN
        self.bearer_token: str = self.DEFAULT_BEARER
        self.live_match_map: Dict[str, dict] = {}
        self.access_token: Optional[str] = None
        self.sportsbook_domain: str = "https://d.bong88.com"
        self._http: Optional[httpx.AsyncClient] = None
        self._scanner_http: Optional[httpx.AsyncClient] = None

    @staticmethod
    def normalize_proxy(raw: str) -> Optional[str]:
        """Chuẩn hóa mọi định dạng Proxy (ip:port:user:pass, user:pass@ip:port, http://, socks5://) sang chuẩn httpx."""
        if not raw:
            return None
        raw = raw.strip()
        if raw.startswith(("http://", "https://", "socks5://")):
            return raw
        if "@" in raw:
            return f"http://{raw}"
        parts = raw.split(":")
        if len(parts) == 4:
            ip, port, user, pw = parts
            return f"http://{user}:{pw}@{ip}:{port}"
        if len(parts) == 2:
            return f"http://{raw}"
        return raw

    def _encrypt_passwords(self, raw_pw: str, mkey: str, ekey: str) -> Tuple[str, str]:
        """Thuật toán mã hóa mật khẩu chuẩn theo Next.js bundle của Bong88 / Viva88."""
        import os
        # 1. Custom hash a(password)
        e_pw = raw_pw + '\x15' * (30 - len(raw_pw)) if (30 - len(raw_pw)) > 1 else raw_pw
        t_val = 1.0
        for a_idx in range(1, 31):
            t_val *= 30 + ord(e_pw[a_idx - 1]) * a_idx
        e_str = f"{t_val:.14E}".replace('+', '')
        i_str = ''.join(hex(int(''.join(str(ord(ch)) for ch in e_str[k - 1:k + 2])))[2:] for k in range(1, len(e_str) + 1))
        hashed_pw = ''.join(i_str[k - 1] for k in range(20, len(i_str) - 17, 2)).upper()

        # 2. RSA PKCS#1 v1.5 encryption
        n_int = int(mkey, 16)
        e_int = int(ekey, 16)
        k_len = (n_int.bit_length() + 7) // 8
        m_bytes = raw_pw.encode('utf-8')
        ps = os.urandom(k_len - len(m_bytes) - 3).replace(b'\x00', b'\x01')
        blk = b'\x00\x02' + ps + b'\x00' + m_bytes
        rsa_pw = pow(int.from_bytes(blk, 'big'), e_int, n_int).to_bytes(k_len, 'big').hex().upper()
        return hashed_pw, rsa_pw

    async def authenticate_member_session(self) -> bool:
        """Đăng nhập lấy AccessToken và Session đặt cược của sảnh thể thao (Sportsbook) qua Proxy HCM."""
        if not self._http:
            await self.login()
        if not self.username or not self.password:
            return False

        try:
            # Lấy rsaPublicKey từ trang chủ
            resp_home = await self._http.get(f"{self.base_url}/b/vn")
            if resp_home.status_code != 200:
                return False

            idx = resp_home.text.find("__NEXT_DATA__")
            if idx == -1:
                return False
            s = resp_home.text[idx:]
            raw_json = s[s.find(">") + 1 : s.find("</script>")]
            data_next = json.loads(raw_json)
            page_props = data_next.get("props", {}).get("pageProps", {})
            rsa_info = page_props.get("rsaPublicKey", {})
            mkey = rsa_info.get("mkey") or rsa_info.get("MKEY")
            ekey = rsa_info.get("ekey") or rsa_info.get("EKEY")
            client_api_url = page_props.get("clientApiUrl", "https://api.bong88.com")

            if not mkey or not ekey:
                return False

            hashed_pw, rsa_pw = self._encrypt_passwords(self.password, mkey, ekey)
            login_payload = {
                "username": self.username,
                "password": hashed_pw,
                "rsaPassword": rsa_pw,
                "isGesture": False,
                "platform": "desktop",
                "language": "vn",
                "loginGuid": "",
                "loginCode": "",
            }

            resp_login = await self._http.post(
                f"{client_api_url}/api/Login",
                json=login_payload,
                headers={"Origin": self.base_url, "Referer": f"{self.base_url}/"},
            )
            login_data = resp_login.json()
            if login_data.get("errorCode") != 0 or not login_data.get("redirectUrl"):
                return False

            # Follow redirectUrl đến d.bong88.com để nhận cookie và trích xuất AccessToken
            redir_url = login_data["redirectUrl"]
            resp_sb = await self._http.get(redir_url)
            if resp_sb.status_code != 200:
                return False

            m_at = re.search(r'MS2\.at\s*=\s*["\']([^"\']+)["\']', resp_sb.text)
            if m_at:
                self.access_token = m_at.group(1)

            m_bcredit = re.search(r'"BCredit"\s*:\s*["\']([0-9.,]+)["\']', resp_sb.text)
            if m_bcredit:
                try:
                    self.detected_balance = float(m_bcredit.group(1).replace(",", ""))
                except Exception:
                    pass

            m_domain = re.search(r'https?://[a-zA-Z0-9.-]*bong88\.com', str(resp_sb.url))
            if m_domain:
                self.sportsbook_domain = m_domain.group(0)

            return bool(self.access_token)
        except Exception:
            return False

    async def login(self) -> bool:
        client_kwargs = {
            "follow_redirects": True,
            "timeout": 10.0,
            "headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
            },
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
        self._http = httpx.AsyncClient(**client_kwargs)
        # Luồng Master Scanner tách biệt (không đốt băng thông Proxy HCM khi quét bảng kèo mỗi 5s)
        self._scanner_http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers=client_kwargs["headers"],
        )

        # Kiểm tra IP đầu ra & độ trễ nếu có cấu hình Proxy HCM
        if self.proxy_url:
            try:
                ip_resp = await self._http.get("https://api.ipify.org?format=json", timeout=6.0)
                if ip_resp.status_code == 200:
                    self.proxy_exit_ip = ip_resp.json().get("ip", "Proxy-OK")
                    self.proxy_healthy = True
            except Exception:
                self.proxy_healthy = False
                self.proxy_exit_ip = "PROXY_ERROR"
                if self.mode == "live" and self.multi_cfg.get("strict_proxy_killswitch", True):
                    return False

        # Tự động thử lần lượt từ bong88.com -> viva88.net để chọn domain phản hồi nhanh nhất tại VN
        for domain in self.candidate_domains:
            try:
                t0 = time.perf_counter()
                resp = await self._http.get(f"{domain}/b/vn")
                ping_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                if resp.status_code == 200:
                    self.base_url = domain
                    self.last_ping_ms = ping_ms
                    m_key = re.search(r'"liveScoreKeyToken"\s*:\s*"([^"]+)"', resp.text)
                    m_bearer = re.search(r'"liveScoreBearerToken"\s*:\s*"([^"]+)"', resp.text)
                    if m_key:
                        self.key_token = m_key.group(1)
                    if m_bearer:
                        self.bearer_token = f"Bearer {m_bearer.group(1)}"
                    self.session_token = f"V88_{self.username}_{uuid.uuid4().hex[:6].upper()}"
                    if self.mode == "live":
                        auth_ok = await self.authenticate_member_session()
                        if not auth_ok and self.multi_cfg.get("strict_proxy_killswitch", True):
                            return False
                    return True
            except Exception:
                continue

        self.session_token = f"V88_{self.username}_{uuid.uuid4().hex[:6].upper()}"
        if self.mode == "live":
            await self.authenticate_member_session()
        return True

    async def close(self) -> None:
        for cli in (self._http, self._scanner_http):
            if cli is not None:
                try:
                    await cli.aclose()
                except Exception:
                    pass
        self._http = None
        self._scanner_http = None

    async def fetch_real_saba_matches(self, running_match_ids: Optional[List[str]] = None) -> List[dict]:
        """Lấy toàn bộ danh sách trận đấu thực tế có trên sảnh Saba của Viva88 hôm nay (kèm tra cứu FT ngày cũ nếu vé vắt qua 11h trưa)."""
        if not self._scanner_http:
            await self.login()
        headers = {
            "Content-Type": "application/json",
            "key": self.key_token,
            "Authorization": self.bearer_token,
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        try:
            resp = await self._scanner_http.post(self.MATCH_API_URL, headers=headers, json={})
            if resp.status_code == 200:
                data = resp.json()
                all_matches = data.get("Result", {}).get("Match", [])
                # Chỉ lọc các trận có mở kèo trên sảnh Saba (SabaId != null) và làm mới bản đồ ngày hiện tại
                saba_matches = [m for m in all_matches if m.get("SabaId")]
                self.live_match_map = {str(m["MatchId"]): m for m in saba_matches}

                # Nếu có vé RUNNING của ngày MeiDonDate hôm trước (vắt qua mốc 11:00 trưa), tra cứu kết quả FT từ API Date/
                if running_match_ids:
                    missing_ids = [mid for mid in running_match_ids if mid not in self.live_match_map]
                    if missing_ids:
                        yest_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                        r_date = await self._scanner_http.post(self.MATCH_DATE_API_URL, headers=headers, json={"Date": yest_str})
                        if r_date.status_code == 200:
                            old_matches = r_date.json().get("Result", {}).get("Match", [])
                            for om in old_matches:
                                om_id = str(om.get("MatchId"))
                                if om_id in missing_ids:
                                    self.live_match_map[om_id] = om
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

    async def fetch_viva88_lines(self, tick: int = 0, running_match_ids: Optional[List[str]] = None) -> List[OddsLine]:
        """
        Quét các trận đấu THẬT đang diễn ra (Live Status 10, 20, 30) và sắp đá (Status 0)
        trên bảng kèo Viva88 (Saba Sports) và tạo dòng kèo sát thực tế cho từng trận.
        """
        saba_matches = await self.fetch_real_saba_matches(running_match_ids=running_match_ids)
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
            # Chỉ vào kèo khi bóng đang lăn thực tế trong Hiệp 1 (10) hoặc Hiệp 2 (30) của 90 phút chính thức.
            # TUYỆT ĐỐI KHÔNG vào kèo lúc nghỉ (Status == 20) vì các giải Cúp hết 90' chuyển sang nghỉ chờ Hiệp Phụ cũng trả Status == 20 (MatchTime == 0)!
            if st not in (10, 30):
                continue
            if int(h_info.get("ExtractScore", 0)) + int(a_info.get("ExtractScore", 0)) > 0:
                continue
            phase = MatchPhase.LIVE
            sh = int(h_info.get("FullScore", 0))
            sa = int(a_info.get("FullScore", 0))
            ch = int(h_info.get("CornerKick", 0))
            ca = int(a_info.get("CornerKick", 0))
            atk_total = int(h_info.get("Attack", 0)) + int(a_info.get("Attack", 0))
            da_total = int(h_info.get("DangerAttack", 0)) + int(a_info.get("DangerAttack", 0))
            sot_total = int(h_info.get("OnGoal", 0)) + int(a_info.get("OnGoal", 0))
            miss_total = int(h_info.get("MissGoal", 0)) + int(a_info.get("MissGoal", 0))
            mt = int(m.get("MatchTime") or 0)
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

            # 2. Kèo Tài Xỉu Phạt Góc (CORNER_OU) nếu trận có thống kê góc (Mốc Saba hạ dần theo phút thi đấu)
            total_corners = ch + ca
            if phase == MatchPhase.LIVE and total_corners > 0:
                mt_val = int(mt or 30)
                if mt_val <= 45:
                    crn_add = 3.5
                elif mt_val <= 56:
                    crn_add = 3.0
                else:
                    crn_add = 2.5
                crn_line = float(total_corners) + crn_add
                viva_hk_crn = 0.76
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
        """Lấy báo giá trực tiếp từ sảnh thể thao Bong88 (qua Proxy HCM) hoặc giả lập tốc độ cao."""
        if self.mode != "live" or not self.access_token or not self._http:
            await asyncio.sleep(0.05)
            return True, signal.viva88_odds, 2000.0

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Referer": f"{self.sportsbook_domain}/",
            "Origin": self.sportsbook_domain,
        }
        try:
            req_data = {
                "ItemList": [{
                    "Oddsid": str(signal.saba_id),
                    "Betteam": "h" if signal.side == "HOME" or signal.side == "OVER" else "a",
                    "Sporttype": 1,
                    "OddsType": 4,  # Decimal
                }],
                "GMT": 7,
            }
            resp = await self._http.post(f"{self.sportsbook_domain}/Betting/GetTickets", headers=headers, json=req_data, timeout=5.0)
            if resp.status_code == 200:
                res_json = resp.json()
                t_list = res_json.get("Data", {}).get("Tickets", [])
                if t_list:
                    ticket_info = t_list[0]
                    live_odds = float(ticket_info.get("DisplayOdds", signal.viva88_odds))
                    max_stake = float(ticket_info.get("MaxBet", 2000.0))
                    min_stake = float(ticket_info.get("MinBet", 3.0))
                    self.detected_min_stake = min_stake
                    return True, live_odds, max_stake
        except Exception:
            pass
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

        # XỬ LÝ ĐẶT CƯỢC THẬT KHI CHẠY MODE "LIVE":
        if self.mode == "live" and self.access_token and self._http:
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Referer": f"{self.sportsbook_domain}/",
                "Origin": self.sportsbook_domain,
            }
            bet_payload = {
                "BetType": "single",
                "ItemList": [{
                    "Oddsid": str(signal.saba_id),
                    "Betteam": "h" if signal.side == "HOME" or signal.side == "OVER" else "a",
                    "Sporttype": 1,
                    "Stake": final_stake,
                    "Odds": live_quote_odds,
                    "AcceptBetterOdds": True,
                }],
                "AutoAccept": True,
            }
            try:
                bet_resp = await self._http.post(f"{self.sportsbook_domain}/Betting/ProcessBet", headers=headers, json=bet_payload, timeout=8.0)
                if bet_resp.status_code == 200:
                    bet_data = bet_resp.json()
                    err = bet_data.get("ErrorCode", 0)
                    if err == 0:
                        real_ticket_ref = bet_data.get("Data", {}).get("TicketId") or ticket_id
                        note = f"LIVE THỰC CHIẾN (Đã khớp vé Saba: {real_ticket_ref})"
                        return BetTicket(
                            ticket_id=str(real_ticket_ref),
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
                            is_simulation=False,
                            note=note,
                        )
                    else:
                        msg = bet_data.get("ErrorMessage") or f"Code {err}"
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
                            is_simulation=False,
                            note=f"Nhà cái từ chối vé ({msg})",
                        )
            except Exception as e:
                pass

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
