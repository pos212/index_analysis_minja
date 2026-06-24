#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSIS OpenAPI에서 세 가지 지수를 받아 data/indices.json 으로 저장한다.
GitHub Actions(또는 임의 서버)에서 실행하는 자동 수집 방식.

- 소비자물가지수      DT_1J22003   (통계청 101, 월)        itmId=T            objL1=T10
- 건설공사비지수      DT_39701_A003 (한국건설기술연구원 397, 월) itmId=16397AAA0   objL1=15397AA2AA
- 건설투자 디플레이터 DT_200Y112    (한국은행 301, 분기)     itmId=13103136282999 objL1=13102136282ACC_ITEM.1020111

KOSIS 개발가이드 준수 사항:
  · 필수변수(apiKey·orgId·tblId·objL1·itmId·prdSe·format) 모두 포함
  · objL2~objL8 은 선택이며, 해당 표에 없는 레벨을 보내면 '오류 21'이 나므로 보내지 않음
  · 기간은 시점기준(startPrdDe+endPrdDe)만 사용(최신자료기준 newEstPrdCnt 와 혼용 금지)
  · 단일 항목·단일 분류만 요청하므로 4만 셀 제한과 무관

네트워크: GitHub 러너는 IPv6 경로에서 KOSIS 접속이 멈추는 경우가 있어 IPv4 를 강제한다.
환경변수: KOSIS_API_KEY(필수), START_YEAR(선택, 기본 2010)
"""

import os, sys, json, time, re, socket, datetime
from urllib.parse import urlencode
from urllib.request import urlopen, Request

# ── IPv4 강제 (러너에서의 timeout/000 방지) ───────────────────────────────
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(host, *args, **kwargs):
    res = _orig_getaddrinfo(host, *args, **kwargs)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res
socket.getaddrinfo = _ipv4_only
# ─────────────────────────────────────────────────────────────────────────

API_KEY    = os.environ.get("KOSIS_API_KEY", "").strip()
BASE       = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
START_YEAR = int(os.environ.get("START_YEAR", "2010"))
NOW        = datetime.date.today()
TIMEOUT    = 120
RETRIES    = 4
OUT_PATH   = os.path.join(os.path.dirname(__file__), "data", "indices.json")

# 라벨에 포함되면 지수 수준값이 아니라 변동률/기여도이므로 제외
EXCLUDE = ("등락률", "증감", "전월", "전년", "기여도", "비중", "전기")

CONFIG = {
    "cpi": {
        "name": "소비자물가지수", "short": "CPI",
        "org_id": "101", "tbl_id": "DT_1J22003", "freq": "M", "unit": "2020=100",
        "itm_id": "T", "obj_l1": "T10",
        "include": ["총지수"], "item_hint": ["지수"],
    },
    "construction": {
        "name": "건설공사비지수", "short": "공사비",
        "org_id": "397", "tbl_id": "DT_39701_A003", "freq": "M", "unit": "2020=100",
        "itm_id": "16397AAA0", "obj_l1": "15397AA2AA",
        "include": ["건설"], "item_hint": ["지수"],
    },
    "deflator": {
        "name": "건설투자 디플레이터", "short": "디플레이터",
        "org_id": "301", "tbl_id": "DT_200Y112", "freq": "Q", "unit": "2020=100",
        "itm_id": "13103136282999", "obj_l1": "13102136282ACC_ITEM.1020111",
        "include": ["건설투자"], "item_hint": ["디플레이터", "지수"],
    },
}


def period_range(freq):
    """전체 기간을 한 번에 요청 (단일 시계열이라 셀 수가 적음)."""
    y = NOW.year
    if freq == "M":
        return f"{START_YEAR}01", f"{y}{NOW.month:02d}"
    if freq == "Q":
        q = (NOW.month - 1) // 3 + 1
        return f"{START_YEAR}1", f"{y}{q}"
    return f"{START_YEAR}", f"{y}"


def fetch(cfg):
    s, e = period_range(cfg["freq"])
    params = {
        "method": "getList", "apiKey": API_KEY,
        "orgId": cfg["org_id"], "tblId": cfg["tbl_id"],
        "itmId": cfg["itm_id"], "objL1": cfg["obj_l1"],   # objL2~8 은 보내지 않음
        "format": "json", "jsonVD": "Y",
        "prdSe": cfg["freq"], "startPrdDe": s, "endPrdDe": e,
    }
    url = BASE + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 kosis-index-tool/1.0"})
    last = None
    for attempt in range(RETRIES):
        try:
            with urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("err"):
                # KOSIS 오류코드: 21 잘못된 요청변수, 30 결과없음, 31/41 초과 등
                raise RuntimeError(f"KOSIS 오류 {data.get('err')}: {data.get('errMsg')}")
            return data if isinstance(data, list) else []
        except Exception as ex:
            last = ex
            wait = 5 * (attempt + 1)
            print(f"  시도 {attempt+1}/{RETRIES} 실패: {ex} → {wait}s 대기", file=sys.stderr)
            time.sleep(wait)
    raise last


def label_of(row):
    return " / ".join(str(row[k]) for k in ("C1_NM", "C2_NM", "C3_NM", "ITM_NM") if row.get(k))


def prd_to_ym(prd, freq):
    digits = re.sub(r"\D", "", str(prd))
    if freq == "M":
        return prd, prd, f"{prd[:4]}.{prd[4:]}"
    if freq == "Q":
        y = digits[:4]; q = int(digits[4]) if len(digits) >= 5 else 1
        return f"{y}{q}", f"{y}{q*3:02d}", f"{y} {q}/4"
    return prd, f"{digits[:4]}12", digits[:4]


def pick_series(cfg, rows):
    inc, hints = cfg["include"], cfg["item_hint"]
    cand, seen = {}, set()
    for row in rows:
        lab = label_of(row); seen.add(lab)
        if any(x in lab for x in EXCLUDE):      continue
        if not all(x in lab for x in inc):      continue
        try:
            val = float(str(row.get("DT")).replace(",", ""))
        except (TypeError, ValueError):         continue
        prd = row.get("PRD_DE")
        if not prd:                             continue
        score = sum(1 for h in hints if h in (row.get("ITM_NM") or ""))
        key, ym, plabel = prd_to_ym(prd, cfg["freq"])
        prev = cand.get(key)
        if prev is None or score > prev[0]:
            cand[key] = (score, val, ym, plabel, lab)

    if not cand:
        print(f"  [경고] '{cfg['name']}' 매칭 실패. 표에 존재하는 라벨(상위 30개):", file=sys.stderr)
        for lab in sorted(seen)[:30]:
            print("    ·", lab, file=sys.stderr)
        return None, None

    matched = max(cand.values(), key=lambda t: t[0])[4]
    series = []
    for key in sorted(cand, key=lambda k: re.sub(r"\D", "", k)):
        _, val, ym, plabel, _ = cand[key]
        if cfg["freq"] == "Q":
            series.append({"period": key, "ym": ym, "label": plabel, "value": round(val, 3)})
        else:
            series.append({"period": key, "value": round(val, 3)})
    return series, matched


def main():
    if not API_KEY:
        print("환경변수 KOSIS_API_KEY 가 비어 있습니다.", file=sys.stderr)
        sys.exit(1)

    indices, ok = {}, True
    for slug, cfg in CONFIG.items():
        print(f"수집: {cfg['name']} ({cfg['tbl_id']}, {cfg['freq']})")
        try:
            rows = fetch(cfg)
        except Exception as ex:
            print(f"  [{cfg['tbl_id']}] 호출 실패: {ex}", file=sys.stderr)
            ok = False
            continue
        series, matched = pick_series(cfg, rows)
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
        print("일부 지수 수집/매칭에 실패했습니다. 위 로그를 확인하세요.", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
