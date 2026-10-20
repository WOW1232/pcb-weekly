"""
주간 업데이트 메인 스크립트.

  python scripts/update.py                  # 오늘(한국시간)이 보고일이면 실행
  python scripts/update.py --date 2026-10-23 --force   # 특정 날짜 강제 실행
  python scripts/update.py --date 2026-10-16 --backtest  # 엑셀에 이미 있는 주차와 비교만
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import sources as S  # noqa: E402

SITE_DATA = ROOT / "site" / "data"
KST = dt.timezone(dt.timedelta(hours=9))
CAL_CODES = {"KR": "XKRX", "JP": "XTKS", "CN": "XSHG", "TW": "XTAI"}
UNIT_FX = {"엔": "JPY", "CNY": "CNY", "NT$": "TWD"}


def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- 달력 ----------------
_cals = {}


def cal(country):
    import exchange_calendars as xcals
    if country not in _cals:
        _cals[country] = xcals.get_calendar(CAL_CODES[country], start="2024-01-01")
    return _cals[country]


def is_session(country, d, extra=()):
    if country == "KR" and d.isoformat() in extra:
        return False
    c = cal(country)  # 라이브러리 오류는 그대로 실패 처리 (휴장일 무시 방지)
    if d > c.last_session.date():
        raise RuntimeError(f"{country} 거래소 달력 범위({c.last_session.date()})를 넘었습니다. 라이브러리 업데이트 필요")
    return bool(c.is_session(d.isoformat()))


def last_session_on_or_before(country, d, extra=()):
    x = d
    for _ in range(20):
        if is_session(country, x, extra):
            return x
        x -= dt.timedelta(days=1)
    return None


def prev_kr_session(d, extra=()):
    return last_session_on_or_before("KR", d - dt.timedelta(days=1), extra)


def is_report_day(d, calcfg):
    """금요일, 또는 금요일까지 남은 날이 모두 휴장이면 그 주의 마지막 한국 거래일"""
    extra = calcfg.get("extra_kr_holidays", [])
    if d.isoformat() in calcfg.get("skip_dates", []):
        return False, "건너뛰기로 지정된 날짜"
    if d.isoformat() in calcfg.get("force_dates", []):
        return True, "강제 실행 날짜"
    if d.weekday() > 4:
        return False, "주말"
    if not is_session("KR", d, extra):
        return False, "한국 휴장일"
    x = d + dt.timedelta(days=1)
    while x.weekday() <= 4:
        if is_session("KR", x, extra):
            return False, f"이번 주 마지막 거래일이 아님 ({x} 거래일)"
        x += dt.timedelta(days=1)
    return True, "이번 주 마지막 한국 거래일"


# ---------------- 종가 수집 ----------------
def collect_close(c, day, extra):
    country = c["country"]
    try:
        expect = last_session_on_or_before(country, day, extra if country == "KR" else ())
    except Exception as e:  # 해외 달력 범위 밖(예: 중국 다음 해 휴일 미발표) → 날짜 검사 생략
        if country == "KR":
            raise
        S.logs.append(f"[달력] {country} {day}: {e} → 수집된 최근 거래일 사용")
        expect = None
    if country == "KR":
        srcs = [("Yahoo", S.yahoo, c["yahoo"]), ("Naver(KRX)", S.naver_kr, c["code"])]
    elif country == "JP":
        srcs = [("Yahoo", S.yahoo, c["yahoo"]), ("Nikkei", S.nikkei_jp, c["code"])]
    elif country == "CN":
        srcs = [("Sina", S.sina_cn, c["code"]), ("Yahoo", S.yahoo, c["yahoo"])]
    else:
        off = S.tpex if c.get("board") == "TPEX" else S.twse
        srcs = [(off.source_name, off, c["code"]), ("Yahoo", S.yahoo, c["yahoo"])]

    found = []
    for name, fn, arg in srcs:
        data = fn(arg, day)
        if not data:
            continue
        d = max(data)                      # 보고일 이전 가장 최근 거래일
        found.append({"source": name, "date": d, "close": data[d]})

    res = {"name": c["name"], "country": country, "expected_date": expect.isoformat() if expect else None,
           "sources": found, "close": None, "date": None, "status": "실패"}
    if not found:
        return res
    # 기대 거래일 데이터가 있는 소스 우선
    good = [f for f in found if f["date"] == res["expected_date"]] or found
    pick = good[0]
    res.update(close=pick["close"], date=pick["date"], picked=pick["source"])
    agree = [f for f in good if f is not pick and f["date"] == pick["date"]
             and abs(f["close"] - pick["close"]) < 0.005]
    disagree = [f for f in good if f is not pick and f["date"] == pick["date"] and f not in agree]
    if res["expected_date"] and res["date"] != res["expected_date"]:
        res["status"] = "날짜확인"
    elif disagree:
        res["status"] = "불일치"
    elif agree:
        res["status"] = "확인"
    else:
        res["status"] = "단일출처"
    return res


def collect_fx(day, extra):
    """보고 전일(직전 한국 거래일) 매매기준율"""
    fx_day = prev_kr_session(day, extra)
    key = os.environ.get("ECOS_KEY") or "sample"
    out = {"date": fx_day.isoformat(), "rates": {}, "detail": {}}
    for cur in ("JPY", "CNY", "TWD"):
        cands = []
        for name, fn in (("SMBS", lambda c, d: S.smbs_fx(c, d)), ("ECOS", lambda c, d: S.ecos_fx(c, d, key))):
            data = fn(cur, fx_day)
            if fx_day.isoformat() in data:
                cands.append({"source": name, "rate": round(data[fx_day.isoformat()], 6)})
        out["detail"][cur] = cands
        out["rates"][cur] = cands[0]["rate"] if cands else None
    return out


# ---------------- 사이트용 데이터 ----------------
def block_to_week(date, rows, statuses=None):
    items = []
    for r in rows:
        fx = r.get("fx") if r.get("unit") in UNIT_FX else None
        mcap = r.get("mcap")
        if mcap is None and r.get("close") is not None and r.get("shares_k") is not None:
            mcap = r["close"] * r["shares_k"] * 1000 / 1e8 * (fx or 1)
        it = {"name": r["name"], "unit": r.get("unit"), "close": r.get("close"),
              "shares_k": r.get("shares_k"), "fx": fx, "mcap": mcap}
        if statuses and r["name"] in statuses:
            it.update({k: statuses[r["name"]].get(k) for k in ("status", "date", "sources", "picked")})
        items.append(it)
    return {"date": date, "items": items}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()

    now = dt.datetime.now(KST)
    day = dt.date.fromisoformat(a.date) if a.date else now.date()
    calcfg = load_json(ROOT / "config" / "calendar.json", {})
    extra = calcfg.get("extra_kr_holidays", [])
    comps = load_json(ROOT / "config" / "companies.json")["companies"]
    status_path = SITE_DATA / "status.json"
    status = {"run_at": now.isoformat(timespec="seconds"), "target_date": day.isoformat()}
    hist = load_json(SITE_DATA / "history.json", [])
    by_date = {h["date"]: h for h in hist}

    ok, why = is_report_day(day, calcfg)
    if not (ok or a.force or a.backtest):
        print(f"{day}: 보고일 아님 – {why}")
        return 0
    last_date = hist[-1]["date"] if hist else ""
    if not a.backtest and last_date > day.isoformat():
        print(f"더 최근 주차({last_date})가 있음 – 건너뜀")
        return 0

    results = [collect_close(c, day, extra) for c in comps]
    fx = collect_fx(day, extra)

    if a.backtest:
        ref = {x["name"].strip(): x for x in by_date.get(day.isoformat(), {}).get("items", [])}
        print(f"=== 백테스트 {day} (기록된 값과 비교) ===")
        bad = 0
        for r in results:
            e = ref.get(r["name"], {}).get("close")
            same = e is not None and r["close"] is not None and abs(e - r["close"]) < 0.005
            bad += 0 if same else 1
            srcs = ", ".join(f"{s['source']}={s['close']}({s['date'][5:]})" for s in r["sources"])
            print(f"{'OK ' if same else 'XX '} {r['name'][:22]:22} 기록={e} 수집={r['close']} [{r['status']}] {srcs}")
        print("FX", fx["rates"], f"불일치 {bad}건")
        print("\n".join(S.logs[-40:]))
        save_json(ROOT / "backtest" / f"{day}.json", {"date": day.isoformat(), "results": results, "fx": fx, "logs": S.logs})
        return 0

    status.update(results=results, fx=fx, logs=S.logs)
    waiting = [r["name"] for r in results if r["close"] is None or r["status"] == "날짜확인"]
    fx_missing = [k for k, v in fx["rates"].items() if v is None]
    # 마감 시각: 보고일 16:25 이후(또는 지난 날짜 수동 실행)면 최종 실행으로 본다
    final = a.force or day < now.date() or (now.hour * 60 + now.minute >= 16 * 60 + 25)

    if waiting or fx_missing:
        if not final:
            status.update(ok=None, pending=waiting,
                          message=f"잠정 – {len(waiting)}종목 확정 대기 (16:30에 다시 수집): {', '.join(waiting)}")
            save_json(status_path, status)
            print(status["message"])
            return 0
        status.update(ok=False, message=f"수집 실패 – 종가: {waiting or '없음'}, 환율: {fx_missing or '없음'}")
        save_json(status_path, status)
        print(status["message"])
        print("\n".join(S.logs))
        return 1

    # 직전 주차(단위·주식수 기준)
    prev = [h for h in hist if h["date"] < day.isoformat()]
    prev_items = {x["name"].strip(): x for x in (prev[-1]["items"] if prev else [])}
    old = by_date.get(day.isoformat())
    # 국내는 KIND 공시로 자동 갱신되는 주식수 연혁의 현재값을 사용
    sh_hist = {x["name"]: x for x in (load_json(SITE_DATA / "shares_history.json", {}) or {}).get("companies", [])}
    rows = []
    for c, r in zip(comps, results):
        p = prev_items.get(c["name"], {})
        unit = p.get("unit")
        kr_now = sh_hist.get(c["name"], {}).get("current") if c["country"] == "KR" else None
        rows.append({"name": c["name"], "unit": unit, "close": r["close"],
                     "shares_k": c.get("shares_k") or (kr_now / 1000 if kr_now else None) or p.get("shares_k"),
                     "fx": fx["rates"].get(UNIT_FX.get(unit)) if unit in UNIT_FX else None, "mcap": None})
    week = block_to_week(day.isoformat(), rows, {r["name"]: r for r in results})
    week["fx_date"] = fx["date"]

    if old:
        oc = {x["name"].strip(): x.get("close") for x in old["items"]}
        changed = [f"{x['name']} {oc.get(x['name'])}→{x['close']}" for x in week["items"]
                   if oc.get(x["name"]) is None or abs(oc[x["name"]] - x["close"]) > 1e-9]
        msg = (f"{day} 주차 재수집 – {len(changed)}건 수정: " + ", ".join(changed)) if changed \
            else f"{day} 주차 재수집 – 변동 없음 (확정)"
    else:
        msg = f"{day} 주차 추가 완료"
    hist = [h for h in hist if h["date"] != week["date"]] + [week]
    save_json(SITE_DATA / "history.json", hist)
    status.update(ok=True, message=msg)
    save_json(status_path, status)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
