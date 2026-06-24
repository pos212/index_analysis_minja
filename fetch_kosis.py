#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSIS OpenAPI에서 세 가지 지수를 받아 data/indices.json 으로 저장한다.
- 소비자물가지수      DT_1J22003 (통계청 101, 월)
- 건설공사비지수      DT_39701_A003 (한국건설기술연구원 397, 월)
- 건설투자 디플레이터 DT_200Y112  (한국은행 301, 분기)  ← '국내총생산에 대한 지출 디플레이터' 중 건설투자

설계 의도:
  정확한 itmId/objL1 코드를 몰라도 되도록 itmId=ALL, objL1~3=ALL 로 받은 뒤
  한글 '항목명/분류값명'을 키워드로 매칭해 필요한 1개 시계열만 골라낸다.
  매칭 실패 시 표에 존재하는 라벨 목록을 출력하므로, 아래 CONFIG의 키워드만 조정하면 된다.
환경변수: KOSIS_API_KEY (필수)
"""

import os, sys, json, time, re, datetime
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

API_KEY = os.environ.get("KOSIS_API_KEY", "").strip()
BASE = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
START_YEAR = int(os.environ.get("START_YEAR", "2010"))     # 수집 시작 연도
THIS_YEAR  = datetime.date.today().year
OUT_PATH   = os.path.join(os.path.dirname(__file__), "data", "indices.json")

# 라벨에 포함되면 '지수 수준값'이 아니라 변동률/기여도이므로 제외
EXCLUDE = ("등락률", "증감", "전월", "전년", "기여도", "비중", "전기")

CONFIG = {
    "cpi": {
        "name": "소비자물가지수", "short": "CPI",
        "org_id": "101", "tbl_id": "DT_1J22003", "freq": "M",
        "unit": "2020=100",
        # 분류값명/항목명 어딘가에 아래 키워드가 모두 들어간 행을 선택
        "include": ["총지수"],
        "item_hint": ["지수"],          # 항목명 보조 힌트(지수 수준값)
    },
    "construction": {
        "name": "건설공사비지수", "short": "공사비",
        "org_id": "397", "tbl_id": "DT_39701_A003", "freq": "M",
        "unit": "2020=100",
        "include": ["총지수"],
        "item_hint": ["지수"],
    },
    "deflator": {
        "name": "건설투자 디플레이터", "short": "디플레이터",
        "org_id": "301", "tbl_id": "DT_200Y112", "freq": "Q",
        "unit": "2020=100",
        "include": ["건설투자"],
        "item_hint": ["디플레이터", "지수"],   # 둘 중 하나라도 맞으면 가점
    },
}


def fetch_period(cfg, start_prd, end_prd, retries=3):
params = {
        "method": "getList", "apiKey": API_KEY,
        "orgId": cfg["org_id"], "tblId": cfg["tbl_id"],
        "itmId": "ALL", "objL1": "ALL",
        "format": "json", "jsonVD": "Y",
        "prdSe": cfg["freq"], "startPrdDe": start_prd, "endPrdDe": end_prd,
    }
    url = BASE + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 kosis-index-tool/1.0"})
    last = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=120) as r:
                raw = r.read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("err"):
                raise RuntimeError(f"KOSIS 오류 {data.get('err')}: {data.get('errMsg')}")
            return data if isinstance(data, list) else []
        except Exception as ex:
            last = ex
            time.sleep(5 * (attempt + 1))   # 5초, 10초, 15초 대기 후 재시도
    raise last


def period_bounds(freq, year):
    if freq == "M":
        return f"{year}01", f"{year}12"
    if freq == "Q":
        return f"{year}1", f"{year}4"
    return f"{year}", f"{year}"


def label_of(row):
    # 분류값명 + 항목명을 합쳐 매칭에 사용
    parts = []
    for k in ("C1_NM", "C2_NM", "C3_NM", "ITM_NM"):
        v = row.get(k)
        if v:
            parts.append(str(v))
    return " / ".join(parts)


def prd_to_ym(prd, freq):
    """KOSIS PRD_DE -> (period_key, ym(분기말월), label)"""
    digits = re.sub(r"\D", "", str(prd))
    if freq == "M":
        return prd, prd, f"{prd[:4]}.{prd[4:]}"
    if freq == "Q":
        y = digits[:4]
        q = int(digits[4]) if len(digits) >= 5 else 1
        return f"{y}{q}", f"{y}{q*3:02d}", f"{y} {q}/4"
    return prd, f"{digits[:4]}12", digits[:4]


def pick_series(cfg, rows):
    inc = cfg["include"]
    hints = cfg["item_hint"]
    # 후보: include 키워드 전부 포함 & EXCLUDE 미포함 & DT 숫자
    cand = {}
    labels_seen = set()
    for row in rows:
        lab = label_of(row)
        labels_seen.add(lab)
        if any(x in lab for x in EXCLUDE):
            continue
        if not all(x in lab for x in inc):
            continue
        dt = row.get("DT")
        try:
            val = float(str(dt).replace(",", ""))
        except (TypeError, ValueError):
            continue
        prd = row.get("PRD_DE")
        if not prd:
            continue
        score = sum(1 for h in hints if h in (row.get("ITM_NM") or ""))
        key, ym, plabel = prd_to_ym(prd, cfg["freq"])
        prev = cand.get(key)
        if prev is None or score > prev[0]:
            cand[key] = (score, val, ym, plabel, lab)

    if not cand:
        print(f"  [경고] '{cfg['name']}' 매칭 실패. 표에 존재하는 라벨(상위 30개):", file=sys.stderr)
        for lab in sorted(labels_seen)[:30]:
            print("    ·", lab, file=sys.stderr)
        return None, None

    matched_label = max(cand.values(), key=lambda t: t[0])[4]
    series = []
    for key in sorted(cand.keys(), key=lambda k: re.sub(r"\D", "", k)):
        _, val, ym, plabel, _ = cand[key]
        if cfg["freq"] == "Q":
            series.append({"period": key, "ym": ym, "label": plabel, "value": round(val, 3)})
        else:
            series.append({"period": key, "value": round(val, 3)})
    return series, matched_label


def collect(cfg):
    rows = []
    for year in range(START_YEAR, THIS_YEAR + 1):
        s, e = period_bounds(cfg["freq"], year)
        try:
            chunk = fetch_period(cfg, s, e)
        except (HTTPError, URLError) as ex:
            print(f"  [{cfg['tbl_id']}] {year} 호출 실패: {ex}", file=sys.stderr)
            chunk = []
        rows.extend(chunk)
        time.sleep(0.3)
    series, matched = pick_series(cfg, rows)
    return series, matched


def main():
    if not API_KEY:
        print("환경변수 KOSIS_API_KEY 가 비어 있습니다.", file=sys.stderr)
        sys.exit(1)

    indices = {}
    ok = True
    for slug, cfg in CONFIG.items():
        print(f"수집: {cfg['name']} ({cfg['tbl_id']}, {cfg['freq']})")
        series, matched = collect(cfg)
        if not series:
            ok = False
            continue
        print(f"  → {len(series)}개 시점, 매칭 라벨: {matched}")
        indices[slug] = {
            "code": cfg["tbl_id"], "org_id": cfg["org_id"],
            "name": cfg["name"], "short": cfg["short"],
            "matched_label": matched, "unit": cfg["unit"], "freq": cfg["freq"],
            "series": series,
        }

    if not indices:
        print("수집된 지수가 없습니다. 종료합니다.", file=sys.stderr)
        sys.exit(2)

    out = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "is_sample": False,
        "base_note": "각 지수의 원자료 기준연도는 표마다 다를 수 있으며, 본 도구는 입력한 '시점'을 100으로 재지수화하여 비교합니다.",
        "indices": indices,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {OUT_PATH}")
    if not ok:
        print("일부 지수 매칭에 실패했습니다. 위 로그의 라벨을 보고 CONFIG의 include 키워드를 조정하세요.", file=sys.stderr)


if __name__ == "__main__":
    main()
