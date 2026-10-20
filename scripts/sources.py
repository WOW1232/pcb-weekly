"""
종가·환율 수집 소스.
각 함수는 {날짜(YYYY-MM-DD): 종가} 형태로 최근 거래일 데이터를 돌려준다.
실패하면 예외 대신 빈 dict를 돌려주고, 원인은 logs에 남긴다.
"""
import datetime as dt
import json
import re
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 20
logs = []


def _f(x):
    if x is None:
        return None
    s = str(x).replace(",", "").strip()
    if s in ("", "--", "-", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _get(url, tries=4, **kw):
    import time
    h = dict(UA)
    h.update(kw.pop("headers", {}))
    for i in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def _safe(name):
    def deco(fn):
        def wrap(*a, **kw):
            try:
                out = fn(*a, **kw) or {}
                if not out:
                    logs.append(f"[{name}] {a} → 데이터 없음")
                return out
            except Exception as e:  # noqa
                logs.append(f"[{name}] {a} → 오류: {type(e).__name__}: {str(e)[:200]}")
                return {}
        wrap.__name__ = fn.__name__
        wrap.source_name = name
        return wrap
    return deco


# ---------------- 공통: 야후 파이낸스 ----------------
@_safe("Yahoo")
def yahoo(ticker, day: dt.date):
    import yfinance as yf
    h = yf.Ticker(ticker).history(start=day - dt.timedelta(days=12), end=day + dt.timedelta(days=1),
                                  interval="1d", auto_adjust=False, actions=False)
    out = {}
    for idx, row in h.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        if d <= day and row["Close"] == row["Close"]:
            out[d.isoformat()] = round(float(row["Close"]), 4)
    return out


# ---------------- 한국: 네이버 (KRX 종가 역산) ----------------
# 네이버 일별 시세의 closePrice 는 NXT 포함 통합가(저녁 8시 마지막 체결)라서 KRX 종가와 다를 수 있다.
# 대신 각 날짜의 '전일대비'는 KRX 종가 기준이므로, 다음 거래일 행에서
#   KRX 종가(D) = 통합종가(D+1) - 전일대비(D+1)
# 로 역산한다. 당일 값은 실시간 API 의 KRX 현재가(장 마감 후 = 종가)를 쓴다.
@_safe("Naver(KRX)")
def naver_kr(code, day: dt.date):
    rows = _get(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=20&page=1").json()
    rows = sorted(rows, key=lambda r: r["localTradedAt"])
    out = {}
    for prev, nxt in zip(rows, rows[1:]):
        d = prev["localTradedAt"][:10]
        if d <= day.isoformat():
            out[d] = _f(nxt["closePrice"]) - _f(nxt["compareToPreviousClosePrice"])
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    if day == now.date() and now.hour * 60 + now.minute >= 15 * 60 + 35:
        j = _get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}").json()
        it = j["datas"][0]
        if it.get("localTradedAt", "")[:10] == day.isoformat():
            out[day.isoformat()] = _f(it["closePrice"])
    return out


# ---------------- 일본: 닛케이 ----------------
@_safe("Nikkei")
def nikkei_jp(code, day: dt.date):
    r = _get(f"https://www.nikkei.com/nkd/company/history/dprice/?scode={code}")
    t = re.sub(r"\s+", " ", r.text)
    out = {}
    for m in re.finditer(r"(\d{1,2})/(\d{1,2})（.）</th>((?: <td[^>]*>[\d,\.\-]+</td>){4})", t):
        mo, d, tds = int(m.group(1)), int(m.group(2)), m.group(3)
        vals = re.findall(r">([\d,\.\-]+)</td>", tds)
        y = day.year if mo <= day.month else day.year - 1
        dd = dt.date(y, mo, d)
        if dd <= day and len(vals) == 4:
            out[dd.isoformat()] = _f(vals[3])
    return out


# ---------------- 중국: 동방재부 / 시나 ----------------
@_safe("Eastmoney")
def eastmoney_cn(code, day: dt.date):
    secid = ("1." if code.startswith("sh") else "0.") + code[2:]
    beg = (day - dt.timedelta(days=15)).strftime("%Y%m%d")
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg={beg}&end={day:%Y%m%d}")
    j = _get(url).json()
    out = {}
    for k in (j.get("data") or {}).get("klines") or []:
        d, c = k.split(",")[:2]
        out[d] = _f(c)
    return out


@_safe("Sina")
def sina_cn(code, day: dt.date):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=15")
    r = _get(url, headers={"Referer": "https://finance.sina.com.cn/"})
    out = {}
    for row in json.loads(r.text):
        d = row["day"][:10]
        if d <= day.isoformat():
            out[d] = _f(row["close"])
    return out


# ---------------- 대만: 거래소 공식 ----------------
def _roc(s):
    y, m, d = s.strip().split("/")
    return dt.date(int(y) + 1911, int(m), int(d))


@_safe("TWSE")
def twse(code, day: dt.date):
    out = {}
    months = {day.replace(day=1)}
    if day.day < 12:
        months.add((day.replace(day=1) - dt.timedelta(days=1)).replace(day=1))
    for m in months:
        j = _get(f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={m:%Y%m%d}&stockNo={code}").json()
        for row in j.get("data") or []:
            d = _roc(row[0])
            if d <= day:
                out[d.isoformat()] = _f(row[6])
    return out


@_safe("TPEx")
def tpex(code, day: dt.date):
    out = {}
    months = {day.replace(day=1)}
    if day.day < 12:
        months.add((day.replace(day=1) - dt.timedelta(days=1)).replace(day=1))
    for m in months:
        rows = []
        try:
            j = _get(f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={code}"
                     f"&date={m:%Y/%m/%d}&response=json").json()
            for t in j.get("tables") or []:
                rows += t.get("data") or []
        except Exception as e:  # noqa
            logs.append(f"[TPEx 신규API] {code} 실패: {e}")
        if not rows:
            roc = f"{m.year - 1911}/{m.month:02d}"
            j = _get("https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php"
                     f"?l=zh-tw&d={roc}&stkno={code}").json()
            rows = j.get("aaData") or []
        for row in rows:
            try:
                d = _roc(str(row[0]).replace("＊", ""))
            except Exception:  # noqa
                continue
            if d <= day:
                out[d.isoformat()] = _f(row[6])
    return out


# ---------------- 환율: 서울외국환중개 매매기준율 ----------------
FX_CODES = {"JPY": ("JPY", 100), "CNY": ("CNH", 1), "TWD": ("TWD", 1)}


@_safe("SMBS")
def smbs_fx(cur, day: dt.date):
    """서울외국환중개 일별 매매기준율. 반환값은 1단위당 원(엔은 100엔 고시 → 1엔으로 환산)."""
    code, per = FX_CODES[cur]
    start = day - dt.timedelta(days=10)
    url = f"http://www.smbs.biz/ExRate/StdExRate_xml.jsp?arr_value={code}_{start}_{day}"
    r = _get(url, headers={"Referer": "http://www.smbs.biz/ExRate/StdExRate.jsp"})
    txt = r.content.decode("utf-8", "ignore")
    out = {}
    # <set label='26.10.15' value='846.65' .../> 형태
    for m in re.finditer(r"label=['\"](\d{2,4})[.\-](\d{1,2})[.\-](\d{1,2})['\"][^>]*?value=['\"]([\d.,]+)['\"]", txt):
        y, mo, d, v = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        out[dt.date(y, int(mo), int(d)).isoformat()] = _f(v) / per
    return out


ECOS_ITEMS = {"JPY": "0000002", "CNY": "0000053", "TWD": None}


@_safe("ECOS")
def ecos_fx(cur, day: dt.date, key="sample"):
    """한국은행 ECOS 731Y001(주요국 통화의 대원화환율, 서울외국환중개 매매기준율/재정환율)"""
    item = ECOS_ITEMS.get(cur)
    if item is None:
        lst = _get(f"https://ecos.bok.or.kr/api/StatisticItemList/{key}/json/kr/1/200/731Y001").json()
        for row in lst["StatisticItemList"]["row"]:
            nm = row.get("ITEM_NAME", "")
            if cur == "TWD" and "대만" in nm:
                item = row["ITEM_CODE"]
        ECOS_ITEMS[cur] = item
    start = day - dt.timedelta(days=10)
    j = _get(f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10/731Y001/D/"
             f"{start:%Y%m%d}/{day:%Y%m%d}/{item}").json()
    out = {}
    for row in j["StatisticSearch"]["row"]:
        d = dt.datetime.strptime(row["TIME"], "%Y%m%d").date()
        v = _f(row["DATA_VALUE"])
        if "100" in row.get("ITEM_NAME1", "") or cur == "JPY":
            v = v / 100
        out[d.isoformat()] = v
    return out
