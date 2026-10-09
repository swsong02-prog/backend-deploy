"""공공기관 채용공고 (재정경제부_공공기관 채용정보 조회서비스 = 잡알리오 데이터)

- 인증키: 환경변수 ALIO_SERVICE_KEY (공공데이터포털 일반 인증키, Decoding)
- 진행 중 공고 전체(수백 건)를 한 번에 받아 2시간 동안 메모리+파일에 캐시
  → 하루 호출 수십 건이라 개발계정 한도(1,000건/일) 안에서 충분
- 받기 실패하면 마지막 캐시를 그대로 쓴다 (화면이 비지 않게)
- 외부 라이브러리 없이 표준 라이브러리만 사용
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

LIST_URL = "https://apis.data.go.kr/1051000/recruitment/list"
CACHE_TTL = 2 * 60 * 60          # 2시간
FAIL_RETRY = 10 * 60             # 실패 후 재시도 간격 10분
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postings_cache.json")

# 우리 13개 직군 → 공공기관 공고의 NCS 대분류
JOB_TO_NCS = {
    "개발": ["정보통신"],
    "데이터·AI": ["정보통신", "연구"],
    "디자인": ["문화.예술.디자인.방송"],
    "마케팅": ["경영.회계.사무", "영업판매", "문화.예술.디자인.방송"],
    "영업": ["영업판매", "경영.회계.사무"],
    "경영사무": ["경영.회계.사무", "사업관리"],
    "금융": ["금융.보험", "경영.회계.사무"],
    "연구·엔지니어링": ["연구", "기계", "전기.전자", "화학", "재료", "건설", "환경.에너지.안전"],
    "공공·행정": ["경영.회계.사무", "사업관리", "법률.경찰.소방.교도.국방", "사회복지.종교"],
    "교육": ["교육.자연.사회과학"],
    "의료·보건": ["보건.의료"],
    "서비스·유통": ["이용.숙박.여행.오락.스포츠", "영업판매", "음식서비스", "운전.운송"],
    "미디어·콘텐츠": ["문화.예술.디자인.방송"],
}

# 세부 직무 → 공고 제목·자격요건에서 찾을 단어
SUB_KEYWORDS = {
    "프론트엔드": ["전산", "IT", "정보", "개발", "소프트웨어", "SW", "웹"],
    "백엔드": ["전산", "IT", "정보", "개발", "소프트웨어", "SW", "서버"],
    "iOS": ["전산", "IT", "앱", "모바일"], "안드로이드": ["전산", "IT", "앱", "모바일"],
    "임베디드": ["전산", "전자", "임베디드", "제어"], "DevOps": ["전산", "IT", "정보", "시스템", "네트워크", "보안"],
    "데이터 분석": ["데이터", "통계", "분석"], "ML·AI": ["AI", "인공지능", "데이터"],
    "데이터 엔지니어": ["데이터", "전산", "정보"], "PM·기획": ["기획", "사업"],
    "인사(HR)": ["인사", "노무"], "총무": ["총무", "행정", "사무"], "재무·회계": ["회계", "재무", "세무"],
    "전략기획": ["기획", "경영"],
    "기계": ["기계"], "전자·전기": ["전기", "전자"], "화공·소재": ["화학", "화공", "소재", "재료"], "연구개발": ["연구"],
    "행정직": ["행정", "사무"], "경찰·소방": ["경찰", "소방", "방호", "경비"], "군인·국방": ["국방", "군"],
    "공기업": ["사무", "행정"], "정책·기획": ["정책", "기획"],
    "교사": ["교사", "교육"], "강사": ["강사", "교육"], "교육기획": ["교육"], "교수·연구": ["연구", "교수"],
    "간호사": ["간호"], "약사": ["약사", "약제"], "보건직": ["보건"], "임상·검사": ["임상", "검사", "병리", "방사선"],
    "PD": ["방송", "영상", "PD"], "기자·에디터": ["홍보", "기자", "편집"], "영상·편집": ["영상", "편집"],
    "작가·기획": ["홍보", "콘텐츠", "기획"],
}

LOCAL_REGIONS = ("대전", "세종", "충남", "충북")

# 화면에서 고르는 지역 → 공고 데이터의 지역 표기 (전남·광주는 "전남광주"로 묶여 오기도 함)
REGION_ALIASES = {
    "대전·충청": ["대전", "세종", "충남", "충북"],
    "광주·전남": ["전남광주", "광주", "전남"],
}
REGIONS = ["서울", "경기", "인천", "대전", "세종", "충남", "충북", "대전·충청", "부산", "대구", "울산",
           "광주·전남", "전북", "경북", "경남", "강원", "제주"]


def _region_tokens(region):
    if not region or region == "전체":
        return []
    return REGION_ALIASES.get(region, [region])

# 공고의 NCS 대분류 → '면접 연습' 눌렀을 때 미리 채울 우리 직군·세부직무
NCS_TO_JOB = [
    ("정보통신", "개발", "백엔드"),
    ("보건.의료", "의료·보건", "보건직"),
    ("금융.보험", "금융", "리스크·심사"),
    ("연구", "연구·엔지니어링", "연구개발"),
    ("전기.전자", "연구·엔지니어링", "전자·전기"),
    ("기계", "연구·엔지니어링", "기계"),
    ("화학", "연구·엔지니어링", "화공·소재"),
    ("재료", "연구·엔지니어링", "화공·소재"),
    ("건설", "연구·엔지니어링", "연구개발"),
    ("환경.에너지.안전", "연구·엔지니어링", "연구개발"),
    ("교육.자연.사회과학", "교육", "교육기획"),
    ("문화.예술.디자인.방송", "미디어·콘텐츠", "작가·기획"),
    ("영업판매", "영업", "영업관리"),
    ("법률.경찰.소방.교도.국방", "공공·행정", "경찰·소방"),
    ("사회복지.종교", "공공·행정", "행정직"),
    ("경영.회계.사무", "공공·행정", "공기업"),
    ("사업관리", "공공·행정", "공기업"),
]


def _suggest_job(ncs, job, sub, ncs_want):
    """사용자 직무가 이 공고 분야에 맞으면 그대로, 아니면 공고 분야로 추정"""
    if job and ncs_want.intersection(ncs):
        return job, sub
    for code, j, sb in NCS_TO_JOB:
        if code in ncs:
            return j, sb
    return "공공·행정", "공기업"

_lock = threading.Lock()
_state = {"items": [], "fetched_at": 0.0, "last_try": 0.0, "error": None}


def _norm(s):
    s = str(s or "").lower()
    s = re.sub(r"\(주\)|㈜|주식회사|\s+", "", s)
    return s


def _parse_ymd(s):
    try:
        return datetime.strptime(str(s), "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _clean(raw):
    end = _parse_ymd(raw.get("pbancEndYmd"))
    return {
        "id": raw.get("recrutPblntSn"),
        "inst": (raw.get("instNm") or "").strip(),
        "title": (raw.get("recrutPbancTtl") or "").strip(),
        "ncs": [x for x in (raw.get("ncsCdNmLst") or "").split(",") if x],
        "region": (raw.get("workRgnNmLst") or "").strip(),
        "hire": (raw.get("hireTypeNmLst") or "").strip(),
        "se": (raw.get("recrutSeNm") or "").strip(),
        "nope": raw.get("recrutNope"),
        "start": raw.get("pbancBgngYmd"),
        "end": raw.get("pbancEndYmd"),
        "end_date": end.isoformat() if end else None,
        "url": (raw.get("srcUrl") or "").strip(),
        "qual": re.sub(r"\s+", " ", (raw.get("aplyQlfcCn") or "")).strip()[:600],
        "pref": re.sub(r"\s+", " ", (raw.get("prefCondCn") or "")).strip()[:300],
    }


def _fetch_all(key):
    q = urllib.parse.urlencode({
        "serviceKey": key, "numOfRows": 1000, "pageNo": 1, "resultType": "json", "ongoingYn": "Y",
    })
    req = urllib.request.Request(f"{LIST_URL}?{q}", headers={"User-Agent": "coachcoach/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8", errors="replace")
    if not body.lstrip().startswith("{"):
        raise RuntimeError("채용정보 API가 JSON이 아닌 응답을 보냈어요 (인증키·트래픽 확인 필요)")
    doc = json.loads(body)
    if str(doc.get("resultCode")) not in ("200", "00", "0"):
        raise RuntimeError(f"채용정보 API 오류: {doc.get('resultCode')} {doc.get('resultMsg')}")
    return [_clean(x) for x in (doc.get("result") or []) if isinstance(x, dict)]


def _load_file():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("items"), list):
            _state["items"] = d["items"]
            _state["fetched_at"] = float(d.get("fetched_at") or 0)
    except (OSError, ValueError):
        pass


def get_postings():
    """진행 중 공고 목록과 갱신 시각. 키가 없으면 빈 목록."""
    key = (os.environ.get("ALIO_SERVICE_KEY") or "").strip()
    now = time.time()
    with _lock:
        if not _state["items"] and not _state["fetched_at"]:
            _load_file()
        stale = now - _state["fetched_at"] > CACHE_TTL
        may_try = now - _state["last_try"] > FAIL_RETRY or not _state["items"]
        if key and stale and may_try:
            _state["last_try"] = now
            try:
                items = _fetch_all(key)
                _state.update(items=items, fetched_at=now, error=None)
                tmp = CACHE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"fetched_at": now, "items": items}, f, ensure_ascii=False)
                os.replace(tmp, CACHE_FILE)
            except Exception as e:  # 네트워크·형식 오류 → 마지막 캐시 유지
                _state["error"] = str(e)[:200]
        today = date.today().isoformat()
        items = [x for x in _state["items"] if not x.get("end_date") or x["end_date"] >= today]
        return items, _state["fetched_at"], _state["error"], bool(key)


def _company_hit(inst_n, companies_n):
    return any(c and len(c) >= 2 and (c in inst_n or inst_n in c) for c in companies_n)


def search(job="", sub="", career="", companies=(), scope="job", limit=8, region=""):
    """scope: job(내 직무) | region(지역) | company(관심 회사). region: REGIONS 중 하나 또는 전체"""
    if scope == "local":  # 예전 화면 호환
        scope, region = "region", "대전·충청"
    rtoks = _region_tokens(region)
    items, fetched_at, error, has_key = get_postings()
    ncs_want = set(JOB_TO_NCS.get(job, []))
    kws = SUB_KEYWORDS.get(sub, [])
    comps = [_norm(c) for c in companies if c]
    today = date.today()

    company_counts = {c: 0 for c in companies if c}
    scored = []
    for x in items:
        inst_n = _norm(x["inst"])
        text = x["title"] + " " + x["qual"]
        is_comp = _company_hit(inst_n, comps)
        if is_comp:
            for c in company_counts:
                cn = _norm(c)
                if len(cn) >= 2 and (cn in inst_n or inst_n in cn):
                    company_counts[c] += 1
        ncs_hit = bool(ncs_want.intersection(x["ncs"]))
        kw_hit = any(k.lower() in text.lower() for k in kws)
        local = bool(rtoks) and any(r in x["region"].split(",") for r in rtoks)

        if scope == "company" and not is_comp:
            continue
        if scope == "region" and rtoks and not local:
            continue
        if scope == "job" and not (ncs_hit or kw_hit or is_comp):
            continue

        s = 0
        s += (100 if scope == "company" else 15) if is_comp else 0
        s += 30 if ncs_hit else 0
        s += 10 if ncs_hit and len(x["ncs"]) <= 2 else 0  # 직무를 좁게 뽑는 공고가 더 정확한 매칭
        s += 25 if kw_hit else 0
        s += 12 if local else 0
        s += 6 if "정규직" in x["hire"] and "비정규직" not in x["hire"] else 0
        if career == "신입" and x["se"] == "경력":
            s -= 40
        if career == "경력" and x["se"] == "신입":
            s -= 15
        end = _parse_ymd(x["end"])
        dday = (end - today).days if end else None
        if dday is not None and dday <= 3:
            s += 3  # 곧 마감은 살짝 위로
        tags = [t for t, ok in (("관심 회사", is_comp), ("내 직무", ncs_hit or kw_hit), (region or "지역", local)) if ok]
        sj, ss = _suggest_job(x["ncs"], job, sub, ncs_want)
        scored.append((s, dday if dday is not None else 999,
                       {**x, "dday": dday, "tags": tags, "suggest_job": sj, "suggest_sub": ss}))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return {
        "items": [t[2] for t in scored[:max(1, min(int(limit or 8), 30))]],
        "total_open": len(items),
        "matched": len(scored),
        "company_counts": company_counts,
        "fetched_at": datetime.utcfromtimestamp(fetched_at).strftime("%Y-%m-%dT%H:%M:%SZ") if fetched_at else None,
        "available": has_key and bool(items),
        "error": error if not items else None,
        "source": "잡알리오(재정경제부 공공기관 채용정보)",
        "regions": REGIONS,
    }
