#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
모듈러 동종사 동향 수집기 (collector.py)
------------------------------------------------
- 네이버 뉴스 검색 API  -> 회사별 모듈러/프리팹 기사 수집 + 이슈 자동 태깅
- DART 오픈API        -> 상장사 3개년 매출/영업이익 수집
- 결과를 dashboard.html 과 동일한 스키마의 JSON 으로 저장

사용법
  1) 패키지 설치:   pip install requests
  2) API 키 설정 (환경변수 권장):
       export NAVER_ID=발급받은_Client_ID
       export NAVER_SECRET=발급받은_Client_Secret
       export DART_KEY=발급받은_DART_API_KEY
  3) 수집:          python collector.py            # dashboard_data.json 생성
  4) 대시보드 연동:  python collector.py --serve     # localhost:8765 로 제공 (대시보드 '로컬 서버 연결')

기존 scheduler.py 통합 시: run_collector() 를 job 으로 등록하세요.
"""

import os, re, sys, json, html, time, datetime, urllib.parse
import requests

# ------------------------------------------------------------------
# 0. 설정
# ------------------------------------------------------------------
NAVER_ID     = os.getenv("NAVER_ID",     "")   # 네이버 개발자센터 Client ID
NAVER_SECRET = os.getenv("NAVER_SECRET", "")   # 네이버 개발자센터 Client Secret
DART_KEY     = os.getenv("DART_KEY",     "")   # opendart.fss.or.kr API Key

OUT_FILE   = "dashboard_data.json"
SINCE_DATE = "2024-01-01"      # 이 날짜 이후 기사만 수집
NEWS_MAX_START = 600           # 쿼리당 최대 페이지 깊이(최신순 100개씩, start 최대치). 600이면 최대 600건까지 과거로 탐색
KEYWORDS   = ["모듈러", "프리팹", "OSC", "탈현장"]   # 회사명과 조합할 키워드

# 추적 대상 회사.  dart_code = DART 고유번호(corp_code) 또는 stock_code(6자리) 아는 경우만.
# stock_code 만 알면 dart_code 는 비워두세요(아래에서 자동 매핑 시도).
COMPANIES = [
    {"name":"삼성물산 건설부문","category":"시공사","stock":"028260"},
    {"name":"현대엔지니어링","category":"시공사","stock":"","dart_name":"현대엔지니어링"},   # 비상장(사업보고서 제출)
    {"name":"현대건설","category":"시공사","stock":"000720"},
    {"name":"DL이앤씨","category":"시공사","stock":"375500"},
    {"name":"GS건설","category":"시공사","stock":"006360"},
    {"name":"포스코이앤씨","category":"시공사","stock":"","dart_name":"포스코이앤씨"},        # 비상장(사업보고서 제출)
    {"name":"유창이앤씨","category":"제작사","stock":"","dart_name":"유창이앤씨"},           # 비상장(감사보고서)
    {"name":"금강공업","category":"제작사","stock":"014280"},
    {"name":"플랜엠","category":"제작사","stock":"","dart_name":"플랜엠"},                   # 비상장
    {"name":"엔알비","category":"제작사","stock":"475230"},                                 # 2025.07 코스닥 상장
    {"name":"스페이스웨이비","category":"제작사","stock":"","dart_name":"스페이스웨이비"},
    {"name":"공간제작소","category":"제작사","stock":"","dart_name":"공간제작소"},           # 비상장(감사보고서)
    {"name":"삼성전자","category":"가전사","stock":"005930"},
    {"name":"LG전자","category":"가전사","stock":"066570"},
    {"name":"간삼건축","category":"설계사","stock":"","dart_name":"간삼건축종합건축사사무소"},
    {"name":"삼우종합건축","category":"설계사","stock":"","dart_name":"삼우종합건축사사무소"},
    {"name":"희림종합건축","category":"설계사","stock":"037440"},                          # 코스닥 상장
]

# 이슈 자동 태깅 규칙 (위에서부터 매칭)
TAG_RULES = [
    ("MOU체결",   ["mou","업무협약","협약 체결","협력 체결","맞손","손잡"]),
    ("입찰·수주", ["수주","낙찰","시공사 선정","도급","착공","준공","공급 계약","납품"]),
    ("특허",      ["특허","출원","등록","신기술","인증"]),
    ("R&D",       ["r&d","연구개발","개발","실증","테스트베드","랩 ","목업","mock"]),
    ("설비투자",  ["공장","인수","증설","생산능력","캐파","투자 유치","펀딩","유니콘"]),
    ("제품출시",  ["출시","공개","신모델","선보","전시","론칭","launch"]),
]

def classify(text):
    t = text.lower()
    for tag, kws in TAG_RULES:
        if any(k in t for k in kws):
            return tag
    return "기타"

def strip_tags(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(s).strip()

# ------------------------------------------------------------------
# 1. 네이버 뉴스 수집
# ------------------------------------------------------------------
def fetch_news():
    if not (NAVER_ID and NAVER_SECRET):
        print("[네이버] API 키 미설정 → 뉴스 수집 건너뜀"); return []
    headers = {"X-Naver-Client-Id":NAVER_ID, "X-Naver-Client-Secret":NAVER_SECRET}
    since = datetime.date.fromisoformat(SINCE_DATE)
    seen, events = set(), []
    for comp in COMPANIES:
        for kw in KEYWORDS:
            q = f'"{comp["name"].split()[0]}" {kw}'
            # 네이버 뉴스는 최신순 100개씩 페이지네이션(start=1,101,...) 가능(최대 1000).
            # SINCE_DATE 이전 기사가 나오면 그 쿼리는 중단(최신순이라 더 과거만 남음).
            start, stop = 1, False
            while start <= NEWS_MAX_START and not stop:
                url = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
                    {"query":q, "display":100, "start":start, "sort":"date"})
                try:
                    r = requests.get(url, headers=headers, timeout=10)
                    if r.status_code != 200:
                        print(f"[네이버] {q} start={start} → HTTP {r.status_code}"); break
                    items = r.json().get("items", [])
                except Exception as e:
                    print(f"[네이버] {q} 실패: {e}"); break
                if not items:
                    break
                for it in items:
                    title = strip_tags(it.get("title"))
                    desc  = strip_tags(it.get("description"))
                    link  = it.get("originallink") or it.get("link")
                    try:
                        dt = datetime.datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z").date()
                    except Exception:
                        dt = None
                    if dt and dt < since:   # 최신순이므로 이 지점부터는 모두 과거 → 이 쿼리 종료
                        stop = True; continue
                    key = (title[:40], link)
                    if key in seen: continue
                    seen.add(key)
                    events.append({
                        "date": dt.isoformat() if dt else "",
                        "type": classify(title + " " + desc),
                        "title": title,
                        "companies": [comp["name"]],
                        "summary": desc,
                        "url": link,
                    })
                start += 100
                time.sleep(0.12)   # rate limit 여유
    # 같은 기사에 여러 회사가 걸리면 병합
    merged = {}
    for e in events:
        k = e["url"] or e["title"]
        if k in merged:
            for c in e["companies"]:
                if c not in merged[k]["companies"]:
                    merged[k]["companies"].append(c)
        else:
            merged[k] = e
    out = sorted(merged.values(), key=lambda x: x["date"], reverse=True)
    print(f"[네이버] 기사 {len(out)}건 수집")
    return out

# ------------------------------------------------------------------
# 2. DART 재무 수집 (상장사 3개년 매출/영업이익)
# ------------------------------------------------------------------
def fetch_financials():
    fin = {}   # name -> {"2022":{rev,op}, ...}
    if not DART_KEY:
        print("[DART] API 키 미설정 → 재무 수집 건너뜀"); return fin
    this_year = datetime.date.today().year
    years = [this_year-3, this_year-2, this_year-1]   # 최근 3개 사업연도
    for comp in COMPANIES:
        code = comp.get("stock")
        if code:
            corp = _corp_code_from_stock(code)
        else:
            corp = _corp_code_from_name(comp.get("dart_name") or comp["name"])
        if not corp:
            if not code:
                print(f"[DART] {comp['name']} - DART 등록명 매칭 실패(수동입력 권장)")
            else:
                print(f"[DART] {comp['name']}({code}) corp_code 매핑 실패")
            continue
        yd_ofs, yd_cfs = {}, {}
        for y in years:
            acc = _dart_accounts(corp, y)   # {"ofs":(rev,op)|None, "cfs":(rev,op)|None}
            if acc.get("ofs"):
                rev, op = acc["ofs"]
                yd_ofs[str(y)] = {k:v for k,v in (("rev",rev),("op",op)) if v is not None}
            if acc.get("cfs"):
                rev, op = acc["cfs"]
                yd_cfs[str(y)] = {k:v for k,v in (("rev",rev),("op",op)) if v is not None}
        if yd_ofs or yd_cfs:
            fin[comp["name"]] = {"ofs": yd_ofs, "cfs": yd_cfs}
            kinds = []
            if yd_ofs: kinds.append("별도")
            if yd_cfs: kinds.append("연결")
            print(f"[DART] {comp['name']} 재무 수집 ({'/'.join(kinds)}) {sorted(set(list(yd_ofs)+list(yd_cfs)))}")
        elif not code:
            print(f"[DART] {comp['name']} - 감사보고서만 있으면 주요계정 API가 비어있을 수 있음(수동입력 권장)")
        time.sleep(0.2)
    return fin

_CORP_MAPS = None   # (stock_code->corp_code, corp_name->corp_code)
def _load_corp_maps():
    """DART corpCode.xml 을 1회 내려받아 stock/name -> corp_code 매핑을 만든다."""
    global _CORP_MAPS
    if _CORP_MAPS is None:
        by_stock, by_name = {}, {}
        try:
            import io, zipfile, xml.etree.ElementTree as ET
            url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_KEY}"
            z = zipfile.ZipFile(io.BytesIO(requests.get(url, timeout=30).content))
            root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
            for e in root.iter("list"):
                sc = (e.findtext("stock_code") or "").strip()
                cc = (e.findtext("corp_code") or "").strip()
                nm = (e.findtext("corp_name") or "").strip()
                if cc and sc and sc != " ":
                    by_stock[sc] = cc
                if cc and nm:
                    by_name.setdefault(nm, cc)
        except Exception as e:
            print(f"[DART] corpCode 매핑 로드 실패: {e}")
        _CORP_MAPS = (by_stock, by_name)
    return _CORP_MAPS

def _corp_code_from_stock(stock):
    return _load_corp_maps()[0].get(stock)

def _corp_code_from_name(name):
    """상장코드 없는 비상장사는 등록명으로 corp_code 조회(완전일치 → 부분일치)."""
    by_name = _load_corp_maps()[1]
    if name in by_name:
        return by_name[name]
    cand = [cc for nm, cc in by_name.items() if name and (name in nm or nm in name)]
    return cand[0] if cand else None

def _dart_accounts(corp_code, year):
    """단일회사 주요계정 → 별도(OFS)/연결(CFS) 각각의 (매출액, 영업이익). 억원 단위 반올림.
       fnlttSinglAcnt 은 fs_div 를 입력으로 받지 않고, 응답의 각 행에 fs_div(OFS/CFS)로
       연결/별도를 함께 내려준다. 따라서 한 번만 호출하고 행별 fs_div 로 나눈다."""
    out = {"ofs": None, "cfs": None}
    url = ("https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?"
           + urllib.parse.urlencode({
               "crtfc_key":DART_KEY, "corp_code":corp_code,
               "bsns_year":str(year), "reprt_code":"11011"}))
    try:
        j = requests.get(url, timeout=10).json()
    except Exception:
        return out
    if j.get("status") != "000":
        return out
    acc = {"OFS":{"rev":None,"op":None}, "CFS":{"rev":None,"op":None}}
    for row in j.get("list", []):
        div = row.get("fs_div","")          # "OFS"(별도) 또는 "CFS"(연결)
        if div not in acc: continue
        nm = row.get("account_nm","")
        amt = (row.get("thstrm_amount","") or "").replace(",","")
        if not amt or not re.match(r"-?\d+$", amt): continue
        val = round(int(amt)/1e8)           # 원 → 억원
        if nm in ("매출액","수익(매출액)"): acc[div]["rev"] = val
        elif nm == "영업이익":            acc[div]["op"]  = val
    if acc["OFS"]["rev"] is not None or acc["OFS"]["op"] is not None:
        out["ofs"] = (acc["OFS"]["rev"], acc["OFS"]["op"])
    if acc["CFS"]["rev"] is not None or acc["CFS"]["op"] is not None:
        out["cfs"] = (acc["CFS"]["rev"], acc["CFS"]["op"])
    return out

# ------------------------------------------------------------------
# 3. 병합 & 저장  (내장 프로필 위에 API 결과를 덮어씀)
# ------------------------------------------------------------------
def load_base_profiles():
    """dashboard.html 의 내장 프로필과 동일. 여기서 정적 프로필을 유지하고
       재무/뉴스만 API 로 갱신합니다. 필요 시 이 목록을 직접 편집하세요."""
    path = os.path.join(os.path.dirname(__file__), "base_profiles.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    # 파일이 없으면 최소 골격만 생성
    return {"companies":[{"name":c["name"],"category":c["category"],"status":"",
             "sectors":"","financials":{},"projects":"","source":""} for c in COMPANIES],
            "events":[], "relations":[]}

def run_collector():
    base = load_base_profiles()
    news = fetch_news()
    fins = fetch_financials()

    # 재무 갱신: 별도(OFS)는 기본 financials에, 연결(CFS)은 financials_cfs에
    for comp in base["companies"]:
        if comp["name"] in fins:
            f = fins[comp["name"]]
            if f.get("ofs"):
                comp["financials"] = f["ofs"]
            elif f.get("cfs") and not comp.get("financials"):
                comp["financials"] = f["cfs"]   # 별도가 없으면 연결로 대체
            if f.get("cfs"):
                comp["financials_cfs"] = f["cfs"]

    # 뉴스 갱신: base 의 수동 이벤트 + API 이벤트 (URL 기준 중복 제거)
    seen = set()
    events = []
    for e in (news + base.get("events", [])):   # API 최신 우선
        k = e.get("url") or e.get("title")
        if k in seen: continue
        seen.add(k); events.append(e)
    events.sort(key=lambda x: x.get("date",""), reverse=True)
    base["events"] = events

    base["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=2)
    print(f"\n✔ 저장 완료 → {OUT_FILE}  (회사 {len(base['companies'])} · 동향 {len(events)})")
    return base

# ------------------------------------------------------------------
# 4. 로컬 서버 (대시보드 연동용, CORS 허용)
# ------------------------------------------------------------------
def serve(port=8765, refresh_hours=3):
    """대시보드 연동 서버. 데이터를 refresh_hours 시간마다 자동으로 다시 수집합니다.
       → 창을 켜두기만 하면 다음 날 대시보드를 열 때 자동으로 최신 데이터가 반영됩니다."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    cache = {"payload": None, "ts": 0.0}
    lock = threading.Lock()
    ttl = refresh_hours * 3600

    def get_payload():
        with lock:
            now = time.time()
            if cache["payload"] is None or (now - cache["ts"]) > ttl:
                print(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M}] 데이터 갱신 중...")
                data = run_collector()
                cache["payload"] = json.dumps(data, ensure_ascii=False).encode("utf-8")
                cache["ts"] = now
            return cache["payload"]

    get_payload()   # 서버 시작 시 1회 수집

    class H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin","*")
        def do_OPTIONS(self):
            self.send_response(204); self._cors()
            self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")
            self.send_header("Access-Control-Allow-Headers","*"); self.end_headers()
        def do_GET(self):
            if self.path.startswith("/api/data"):
                payload = get_payload()   # TTL 지나면 자동 재수집
                self.send_response(200); self._cors()
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.end_headers(); self.wfile.write(payload)
            else:
                self.send_response(404); self._cors(); self.end_headers()
        def log_message(self,*a): pass

    print(f"\n▶ 대시보드 연동 서버 실행 중 → http://localhost:{port}/api/data")
    print(f"  데이터는 {refresh_hours}시간마다 자동 갱신됩니다. (이 창을 열어두세요. Ctrl+C 로 종료)")
    HTTPServer(("127.0.0.1", port), H).serve_forever()

# ------------------------------------------------------------------
if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve()
    else:
        run_collector()
