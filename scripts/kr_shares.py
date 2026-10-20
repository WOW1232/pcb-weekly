"""
국내 상장사 주식수 연혁 자동 갱신.
KIND(거래소 공시)에서 '추가상장'·'변경상장' 공시를 찾아 원문의 추가/변경 주식수와
상장 후 총주식수를 읽어 site/data/shares_history.json 에 추가한다.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "site" / "data" / "shares_history.json"
KIND = "https://kind.krx.co.kr"
VIEW = KIND + "/common/disclsviewer.do?method=search&acptno="
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept-Language": "ko-KR,ko;q=0.9"}
LABEL = {"국내사모 전환사채 전환": "CB 전환", "국내사모 신주인수권부사채 신주인수권 행사": "BW 행사",
         "우선주의 보통주전환": "우선주→보통주 전환", "주식소각": "자기주식 소각", "무상증자": "무상증자",
         "유상증자(구주주배정)": "유상증자(주주배정)", "주식매수선택권행사": "스톡옵션 행사", "합병": "합병 신주"}


def _n(s):
    return int(s.replace(",", ""))


def search(sess, code, frm, to):
    rows = []
    for kw in ("추가상장", "변경상장"):
        r = sess.post(KIND + "/disclosure/details.do",
                      data={"method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1",
                            "orderMode": "1", "orderStat": "D", "forward": "details_sub",
                            "repIsuSrtCd": "A" + code, "isurCd": code[:5],
                            "fromDate": frm, "toDate": to, "reportNm": kw},
                      headers={"Referer": KIND + "/disclosure/details.do?method=searchDetailsMain",
                               "X-Requested-With": "XMLHttpRequest"}, timeout=25)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
            a = re.search(r"openDisclsViewer\('(\d+)'", tr)
            tds = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if a and len(tds) >= 4 and re.search(r"추가상장|변경상장", tds[3]):
                rows.append({"acptno": a.group(1), "time": tds[1], "title": tds[3]})
        time.sleep(0.4)
    return rows


def body(sess, acptno):
    r = sess.get(f"{KIND}/common/disclsviewer.do?method=searchInitInfo&acptNo={acptno}&docno=", timeout=25)
    m = re.search(r"<option value=['\"](\d+)\|", r.text)
    if not m:
        return ""
    r2 = sess.get(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}", timeout=25)
    p = re.search(r"(/external/[^'\"]+\.htm)", r2.text)
    if not p:
        return ""
    r3 = sess.get(KIND + p.group(1), timeout=25)
    r3.encoding = r3.apparent_encoding or "utf-8"
    h = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", r3.text)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", h))


def parse(title, t):
    if "변경상장" in title:
        m = re.search(r"([\d,]+)주 → ([\d,]+)주", t)
        ch = re.search(r"변경주식수 : ([\-\d,]+)주", t)
        d = re.search(r"변경상장일 : (\d{4})년(\d{2})월(\d{2})일", t)
        why = re.search(r"변경사유 : ([^⑨|]+)", t)
        return {"date": "-".join(d.groups()) if d else None, "reason": why.group(1).strip() if why else title,
                "change": _n(ch.group(1)) if ch else None, "after": _n(m.group(2)) if m else None}
    add = re.search(r"추가주식수\(주\) \| \| \| \| 보통주 \| \| A\d+ \| \| ([\d,]+)", t)
    tot = re.search(r"상장주식총수 .*?보통주 \| \| ([\d,]+)", t)
    d = re.search(r"5\.상장일 \| \| (\d{4}-\d{2}-\d{2})", t)
    why = re.search(r"추가사유 .*?\| \| 보통주 \| \| ([^|]+?) \|", t)
    if not add:  # 다른 서식: ① 주식의 종류와 수 : 기명식 보통주 361,873주
        add = re.search(r"보통주 ([\d,]+)주", t)
        d = d or re.search(r"상장일 : (\d{4})년(\d{2})월(\d{2})일", t)
        why = why or re.search(r"증자방법 : ([^⑥|]+)", t)
    date = None
    if d:
        date = d.group(1) if len(d.groups()) == 1 else "-".join(d.groups())
    return {"date": date, "reason": why.group(1).strip() if why else title,
            "change": _n(add.group(1)) if add else None, "after": _n(tot.group(1)) if tot else None}


def main():
    hist = json.loads(HIST.read_text(encoding="utf-8"))
    comps = json.loads((ROOT / "config" / "companies.json").read_text(encoding="utf-8"))["companies"]
    codes = {c["name"]: c["code"] for c in comps if c["country"] == "KR"}
    sess = requests.Session(); sess.headers.update(UA)
    sess.get(KIND + "/disclosure/details.do?method=searchDetailsMain", timeout=20)
    to = dt.date.today()
    frm = to - dt.timedelta(days=45)
    added = []
    for c in hist["companies"]:
        if c["name"] not in codes:
            continue
        seen = {e.get("source", "").split("acptno=")[-1] for e in c["events"]} | {a for e in c["events"] for a in e.get("acptnos", [])}
        try:
            rows = search(sess, codes[c["name"]], frm.isoformat(), to.isoformat())
        except Exception as e:  # noqa
            print("검색 실패", c["name"], e); continue
        for r in rows:
            if r["acptno"] in seen:
                continue
            try:
                p = parse(r["title"], body(sess, r["acptno"]))
            except Exception as e:  # noqa
                print("원문 실패", c["name"], r["acptno"], e); continue
            reason = p["reason"]
            if "추가상장(" in reason:
                reason = reason.split("(", 1)[1].rstrip(")")
            ev = {"date": p["date"] or r["time"][:10], "type": LABEL.get(reason, reason), "change": p["change"],
                  "after": p["after"], "source": VIEW + r["acptno"]}
            c["events"].append(ev); seen.add(r["acptno"]); added.append((c["name"], ev))
            time.sleep(0.4)
        c["events"].sort(key=lambda e: e["date"])
        afters = [e for e in c["events"] if e.get("after")]
        if afters:
            c["current"] = afters[-1]["after"]
    if added:
        hist["updated"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
        HIST.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    for n, e in added:
        print("추가:", n, e["date"], e["type"], e["change"], e["after"])
    print(f"국내 주식수 공시 {len(added)}건 추가")


if __name__ == "__main__":
    main()
