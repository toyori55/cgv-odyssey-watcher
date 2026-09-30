#!/usr/bin/env python3
"""CGV 신규 예매일 감시 -> 텔레그램 알림. (여러 극장 동시 감시)

CGV는 Cloudflare 봇 차단을 쓰기 때문에 일반 HTTP 클라이언트(requests/curl)는 403이 난다.
curl_cffi 로 크롬의 TLS 지문을 흉내내면 통과한다. (헤드리스 브라우저 불필요)
"""
import json
import os
import sys
import time
from datetime import datetime, date, timezone, timedelta

from curl_cffi import requests

KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

BASE = "https://cgv.co.kr/api/v1/booking"
CO_CD = "A420"

MOV_NO = os.environ.get("CGV_MOV_NO", "30001323")
MOV_NAME = os.environ.get("CGV_MOV_NAME", "오디세이")


def parse_sites(raw):
    """'0074:CGV 왕십리,0013:CGV 용산아이파크몰' -> [('0074','CGV 왕십리'), ...]"""
    sites = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        code, _, name = chunk.partition(":")
        sites.append((code.strip(), name.strip() or code.strip()))
    return sites


SITES = parse_sites(os.environ.get(
    "CGV_SITES", "0074:CGV 왕십리,0013:CGV 용산아이파크몰"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.environ.get(
    "CGV_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://cgv.co.kr/cnm/movieBook/movie",
    "Origin": "https://cgv.co.kr",
}

BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"
MOVIE_URL = f"https://cgv.co.kr/cnm/cgvChart/movieChart/{MOV_NO}"

# 알림에 붙는 인라인 버튼. 텔레그램은 http(s) 링크만 버튼에 허용한다.
# (CGV는 앱 딥링크를 제공하지 않아 앱으로 바로 여는 버튼은 만들 수 없다.)
ALERT_BUTTONS = [[{"text": "🎟 지금 예매하기", "url": BOOKING_URL}],
                 [{"text": "🎬 영화 정보", "url": MOVIE_URL}]]

FAIL_ALERT_AFTER = 10  # 이 횟수만큼 연속 실패하면 한 번 경고
RETRY_AFTER_MAX_WAIT = 120  # 429 때 이 시간까지는 기다렸다 재시도, 넘으면 다음 회차로 미룸


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def api_get(session, path, params, tries=3):
    """CGV API 호출. 일시적 실패는 재시도."""
    last = None
    for attempt in range(tries):
        try:
            r = session.get(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                body = r.json()
                if body.get("statusCode") == 0:
                    return body.get("data") or []
                last = f"statusCode={body.get('statusCode')} {body.get('statusMessage')}"
            else:
                last = f"HTTP {r.status_code}"
        except Exception as e:  # 네트워크/파싱 오류
            last = f"{type(e).__name__}: {e}"
        if attempt < tries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{path} 실패: {last}")


def fetch_open_dates(session, site_no):
    """이 극장에서 이 영화의 '예매 가능한 날짜' 목록 (YYYYMMDD)."""
    data = api_get(session, "searchSiteScnscYmdListByMov",
                   {"coCd": CO_CD, "siteNo": site_no, "movNo": MOV_NO})
    return sorted({row["scnYmd"] for row in data if row.get("scnYmd")})


def fetch_showtimes(session, site_no, ymd):
    """특정 날짜의 상영 회차 목록."""
    try:
        return api_get(session, "searchSchByMov",
                       {"coCd": CO_CD, "siteNo": site_no, "scnYmd": ymd,
                        "movNo": MOV_NO, "rtctlScopCd": "08"}, tries=2)
    except Exception as e:
        log(f"  상영시간표 조회 실패({site_no}/{ymd}): {e}")
        return []


def fmt_date(ymd):
    d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    return f"{d:%Y-%m-%d} ({WEEKDAY_KR[d.weekday()]})"


def fmt_time(hhmm):
    return f"{hhmm[:2]}:{hhmm[2:]}" if hhmm and len(hhmm) == 4 else (hhmm or "")


def fmt_showtimes(rows):
    """상영관별로 묶어서 사람이 읽을 수 있게."""
    if not rows:
        return "  (상영시간표는 아직 조회되지 않음 — 앱에서 확인)"
    by_screen = {}
    for r in rows:
        key = r.get("expoScnsNm") or r.get("scnsNm") or "상영관"
        by_screen.setdefault(key, []).append(r)
    lines = []
    for screen, items in by_screen.items():
        items.sort(key=lambda r: r.get("scnsrtTm") or "")
        kind = items[0].get("movkndDsplNm") or ""
        head = f"  🎞 {screen}" + (f" · {kind}" if kind and kind not in screen else "")
        lines.append(head)
        for r in items:
            start = fmt_time(r.get("scnsrtTm"))
            free = r.get("frSeatCnt")
            total = r.get("cpSeatCnt") or r.get("stcnt")
            seat = f" — 잔여 {free}/{total}석" if free is not None and total else ""
            lines.append(f"     {start}{seat}")
    return "\n".join(lines)


TELEGRAM_MAX = 4096
SAFE_LEN = 3800  # 여유를 두고 자른다


def build_messages(header, blocks):
    """헤더 + 날짜별 블록을 텔레그램 길이 제한에 맞게 1개 이상의 메시지로 나눈다.

    새 날짜가 한꺼번에 여러 개 열리고 상영관이 많으면 4096자를 넘길 수 있는데,
    그러면 전송이 통째로 실패해 알림을 놓친다. 그래서 미리 쪼갠다.
    """
    msgs, cur = [], []
    cur_len = len(header)
    for b in blocks:
        # 블록 하나가 이미 너무 길면 그 블록만 잘라낸다.
        if len(b) > SAFE_LEN - len(header):
            b = b[: SAFE_LEN - len(header) - 40].rstrip() + "\n     … (이하 생략, 앱에서 확인)"
        if cur and cur_len + len(b) + 2 > SAFE_LEN:
            msgs.append(header + "\n\n" + "\n\n".join(cur))
            cur, cur_len = [], len(header)
        cur.append(b)
        cur_len += len(b) + 2
    if cur:
        msgs.append(header + "\n\n" + "\n\n".join(cur))
    return msgs


def send_telegram(text, silent=False, buttons=None):
    if not BOT_TOKEN or not CHAT_ID:
        log("텔레그램 토큰/챗ID가 없어 전송 생략. 메시지 내용:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                # 텔레그램 속도 제한. 얼마나 기다려야 하는지 알려준다.
                try:
                    wait = int((r.json().get("parameters") or {}).get("retry_after") or 0)
                except Exception:
                    wait = 0
                if wait > RETRY_AFTER_MAX_WAIT:
                    # 오래 기다려야 하면 이 회차는 포기한다. 기준선을 갱신하지 않으므로
                    # 바깥 1분 루프가 다음 회차에 자동으로 다시 시도한다.
                    log(f"텔레그램 속도제한(429) — {wait}초 대기 필요. "
                        f"이번 회차 포기, 다음 회차에 재시도합니다.")
                    return False
                log(f"텔레그램 속도제한(429) — {wait}초 기다린 뒤 재시도")
                time.sleep(wait + 1)
                continue
            log(f"텔레그램 전송 실패 HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"텔레그램 전송 오류: {type(e).__name__}: {e}")
        time.sleep(2 * (attempt + 1))
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"sites": {}}
    except Exception as e:
        log(f"상태파일 손상({e}) — 새로 시작")
        return {"sites": {}}

    # 예전 단일극장 형식(state["known_dates"])을 극장별 형식으로 옮긴다.
    if "known_dates" in state and "sites" not in state:
        first = SITES[0][0] if SITES else "0074"
        state = {"sites": {first: {
            "known_dates": state["known_dates"],
            "consecutive_failures": state.get("consecutive_failures", 0),
            "failure_alerted": state.get("failure_alerted", False),
        }}}
        log(f"이전 상태를 극장 {first} 기준으로 이전했습니다.")
    state.setdefault("sites", {})
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def check_site(session, site_no, site_name, st):
    """극장 하나를 확인한다. st 는 이 극장의 상태 dict (제자리에서 갱신)."""
    try:
        dates = fetch_open_dates(session, site_no)
    except Exception as e:
        fails = st.get("consecutive_failures", 0) + 1
        st["consecutive_failures"] = fails
        log(f"[{site_name}] 조회 실패({fails}회 연속): {e}")
        if fails == FAIL_ALERT_AFTER and not st.get("failure_alerted"):
            send_telegram(
                f"⚠️ CGV 감시 오류\n{MOV_NAME} / {site_name} 조회가 "
                f"{FAIL_ALERT_AFTER}회 연속 실패했습니다.\n사유: {e}\n\n"
                f"CGV가 차단 방식을 바꿨을 수 있습니다."
            )
            st["failure_alerted"] = True
        return

    if st.get("consecutive_failures", 0) >= FAIL_ALERT_AFTER and st.get("failure_alerted"):
        send_telegram(f"✅ CGV 감시 정상 복구 ({MOV_NAME} / {site_name})", silent=True)
    st["consecutive_failures"] = 0
    st["failure_alerted"] = False

    # 첫 실행: 지금 열려 있는 날짜를 기준선으로 저장만 하고 알리지 않는다.
    if "known_dates" not in st:
        st["known_dates"] = dates
        log(f"[{site_name}] 기준선 저장: {len(dates)}일 "
            f"({dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'})")
        send_telegram(
            f"👀 감시 시작\n🏛 {site_name}\n🎬 {MOV_NAME}\n\n"
            f"현재 열린 예매일: {len(dates)}일\n"
            f"{fmt_date(dates[0]) if dates else '-'} ~ "
            f"{fmt_date(dates[-1]) if dates else '-'}\n\n"
            f"새 날짜가 열리면 바로 알려드릴게요.",
            silent=True,
        )
        return

    known = set(st["known_dates"])
    target_date = "20261003"
    new_dates = [d for d in dates if d not in known and d == target_date]

    # 10/3의 현재 상영 회차를 "상영관 + 시작시간"으로 구별해서 기억한다.
    target_rows = fetch_showtimes(session, site_no, target_date) if target_date in dates else []
    current_showtimes = {
        f"{r.get('expoScnsNm') or r.get('scnsNm') or '상영관'}|{r.get('scnsrtTm') or ''}"
        for r in target_rows
        if r.get("scnsrtTm")
    }

    previous_showtimes = set(st.get("known_showtimes_20261003", []))
    added_showtimes = current_showtimes - previous_showtimes

    # 10/3 날짜가 처음 열렸을 때
    if new_dates:
        log(f"[{site_name}] 🔔 10/3 예매 오픈")
        header = (f"🔔 10월 3일 예매가 열렸습니다!\n"
                  f"🏛 {site_name}\n🎬 {MOV_NAME}")
        blocks = [f"📅 {fmt_date(target_date)}\n{fmt_showtimes(target_rows)}"]
        msgs = build_messages(header, blocks)

        for i, msg in enumerate(msgs):
            if i:
                time.sleep(1.2)
            if not send_telegram(msg, buttons=ALERT_BUTTONS):
                log(f"[{site_name}] 전송 실패 — 다음 회차에 재시도")
                return

    # 이미 10/3이 열려 있고, 나중에 새로운 회차가 추가된 경우
    elif target_date in dates and previous_showtimes and added_showtimes:
        new_rows = [
            r for r in target_rows
            if f"{r.get('expoScnsNm') or r.get('scnsNm') or '상영관'}|{r.get('scnsrtTm') or ''}"
            in added_showtimes
        ]

        log(f"[{site_name}] 🔔 신규 상영회차 {len(added_showtimes)}개")
        msg = (f"🔔 새로운 상영회차가 추가됐습니다!\n"
               f"🏛 {site_name}\n🎬 {MOV_NAME}\n\n"
               f"📅 {fmt_date(target_date)}\n"
               f"{fmt_showtimes(new_rows)}")

        if not send_telegram(msg, buttons=ALERT_BUTTONS):
            log(f"[{site_name}] 전송 실패 — 다음 회차에 재시도")
            return

    else:
        log(f"[{site_name}] 변화 없음 ({len(dates)}일, 마지막 {dates[-1] if dates else '-'})")

    st["known_dates"] = dates
    st["known_showtimes_20261003"] = sorted(current_showtimes)


def run_once(session):
    state = load_state()
    for site_no, site_name in SITES:
        st = state["sites"].setdefault(site_no, {})
        try:
            check_site(session, site_no, site_name, st)
        except Exception as e:
            log(f"[{site_name}] 예상치 못한 오류: {type(e).__name__}: {e}")
    state["last_checked"] = datetime.now(KST).isoformat(timespec="seconds")
    save_state(state)
    return 0


def send_test_alert(session):
    """실제 알림이 어떻게 생겼는지 확인용. (감시 기준선은 건드리지 않음)"""
    ok_all = True
    for site_no, site_name in SITES:
        dates = fetch_open_dates(session, site_no)
        if not dates:
            log(f"[{site_name}] 열린 날짜가 없어 테스트 알림 생략")
            continue
        ymd = dates[-1]
        msg = (f"🧪 [테스트] 실제 알림은 이렇게 옵니다\n\n"
               f"🔔 새 예매일이 열렸습니다! (1일)\n"
               f"🏛 {site_name}\n🎬 {MOV_NAME}\n\n"
               f"📅 {fmt_date(ymd)}\n{fmt_showtimes(fetch_showtimes(session, site_no, ymd))}")
        ok = send_telegram(msg, buttons=ALERT_BUTTONS)
        log(f"[{site_name}] 테스트 알림 전송 " + ("성공" if ok else "실패"))
        ok_all = ok_all and ok
    return 0 if ok_all else 1


def run_diagnostics():
    """메시지를 보내지 않고 봇/채팅 상태만 조회한다. (429 원인 좁히기용)

    sendMessage 가 아닌 읽기 전용 API 들이라, 봇 전체가 막힌 건지
    특정 채팅으로 보내는 것만 막힌 건지 구분할 수 있다.
    토큰은 절대 출력하지 않는다.
    """
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    checks = [
        ("getMe", "/getMe", {}),
        ("getWebhookInfo", "/getWebhookInfo", {}),
        ("getChat(대상 채팅)", "/getChat", {"chat_id": CHAT_ID}),
        ("getChatMemberCount", "/getChatMemberCount", {"chat_id": CHAT_ID}),
    ]
    for label, path, params in checks:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and body.get("ok"):
                res = body.get("result")
                if isinstance(res, dict):
                    keep = {k: v for k, v in res.items()
                            if k in ("username", "type", "title", "pending_update_count",
                                     "last_error_message", "last_error_date", "url",
                                     "can_post_messages", "id")}
                    log(f"  ✅ {label}: {keep}")
                else:
                    log(f"  ✅ {label}: {res}")
            else:
                params_out = (body.get("parameters") or {})
                log(f"  ❌ {label}: HTTP {r.status_code} "
                    f"{body.get('description', r.text[:120])} {params_out}")
        except Exception as e:
            log(f"  ❌ {label}: {type(e).__name__}: {e}")
    return 0


def main():
    session = requests.Session(impersonate="chrome")
    log("감시 극장: " + ", ".join(f"{n}({c})" for c, n in SITES))

    if os.environ.get("CGV_DIAGNOSE") == "1":
        log("텔레그램 진단 시작 (메시지는 보내지 않습니다)")
        return run_diagnostics()

    if os.environ.get("CGV_TEST_ALERT") == "1":
        return send_test_alert(session)

    interval = int(os.environ.get("CGV_LOOP_INTERVAL", "0"))
    duration_min = float(os.environ.get("CGV_LOOP_DURATION_MIN", "0"))

    # 단발 실행 모드
    if interval <= 0:
        return run_once(session)

    # 루프 모드: GitHub Actions 처럼 1분 크론이 없는 환경에서 잡 안에서 반복한다.
    deadline = time.monotonic() + duration_min * 60
    log(f"루프 시작 — {interval}초 간격, 약 {duration_min:g}분 동안")
    n = 0
    while True:
        started = time.monotonic()
        n += 1
        # 몇 시간씩 도는 잡이라 오래된 연결이 끊길 수 있다. 주기적으로 새로 맺는다.
        if n % 60 == 0:
            session = requests.Session(impersonate="chrome")
            log(f"({n}회차) 연결 갱신")
        try:
            run_once(session)
        except Exception as e:  # 루프 자체는 절대 죽지 않게
            log(f"예상치 못한 오류: {type(e).__name__}: {e}")
        if time.monotonic() + interval > deadline:
            log(f"루프 종료 — 총 {n}회 확인")
            return 0
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
