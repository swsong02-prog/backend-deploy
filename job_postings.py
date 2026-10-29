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
    "클라우드·인프라": ["정보통신"],
    "정보보안": ["정보통신"],
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
    "풀스택": ["전산", "IT", "개발", "웹"], "게임 개발": ["게임"], "QA·테스트": ["품질", "시험", "테스트"],
    "클라우드 엔지니어": ["클라우드", "전산", "인프라", "서버"], "클라우드 아키텍트": ["클라우드", "아키텍처", "인프라"],
    "SRE": ["시스템", "운영", "인프라", "전산"], "시스템 엔지니어": ["시스템", "서버", "전산", "운영"],
    "네트워크 엔지니어": ["네트워크", "통신", "정보통신"], "DBA": ["DB", "데이터베이스"],
    "보안 관제": ["보안", "관제"], "모의해킹": ["보안", "해킹", "취약점"], "보안 컨설팅": ["보안", "컨설팅"],
    "정보보호 관리": ["정보보호", "보안", "ISMS"], "클라우드 보안": ["보안", "클라우드"],
    "개인정보보호": ["개인정보", "정보보호"],
    "데이터 사이언티스트": ["데이터", "통계", "분석", "AI"], "AI 엔지니어(LLM)": ["AI", "인공지능", "LLM"],
    "브랜드 디자인": ["디자인", "브랜드"], "웹 디자인": ["디자인", "웹"],
    "CRM": ["고객", "CRM", "마케팅"], "홍보·PR": ["홍보", "언론", "대외협력"],
    "기술영업": ["기술영업", "영업", "사업화"], "영업지원": ["영업", "지원"],
    "법무": ["법무", "법률", "변호사", "계약"], "구매·자재": ["구매", "자재", "조달", "계약"],
    "서비스 기획": ["기획", "서비스"], "사무·비서": ["사무", "비서", "행정"],
    "핀테크": ["금융", "IT", "디지털"], "세무·회계사": ["세무", "회계사", "회계"],
    "반도체": ["반도체"], "건축·토목": ["건축", "토목", "건설"], "생산·품질관리": ["생산", "품질"],
    "환경·안전": ["환경", "안전", "보건관리"],
    "기술직 공무원": ["기술", "시설", "전기", "기계", "토목"], "사회복지": ["사회복지", "복지"],
    "유아·보육": ["보육", "유아", "어린이집"], "상담": ["상담", "심리"],
    "방사선사": ["방사선"], "물리·작업치료": ["물리치료", "작업치료", "재활"], "영양사": ["영양", "조리"],
    "의료행정": ["원무", "의무기록", "의료"],
    "물류·SCM": ["물류", "유통", "운송"], "고객상담(CS)": ["상담", "고객", "콜센터"], "여행·관광": ["관광", "여행"],
    "아나운서": ["아나운서", "방송"], "게임 기획": ["게임", "콘텐츠"],
    # 탐색 화면에서 세부 직무를 고를 수 있게 나머지 세부직무도 단어를 붙여 둔다
    "UI·UX": ["UI", "UX", "디자인"], "그래픽": ["디자인", "그래픽"], "제품 디자인": ["디자인", "제품"],
    "영상·모션": ["영상", "모션"],
    "브랜드": ["홍보", "브랜드"], "퍼포먼스": ["마케팅", "광고"], "콘텐츠": ["콘텐츠", "홍보"], "SNS": ["SNS", "홍보", "온라인"],
    "B2B 영업": ["영업", "판로", "마케팅"], "B2C 영업": ["영업", "고객"], "해외영업": ["해외", "무역", "수출"],
    "영업관리": ["영업", "판매"],
    "은행": ["금융", "여신", "수신"], "증권·투자": ["투자", "증권", "자산운용"], "보험": ["보험"],
    "리스크·심사": ["심사", "리스크", "신용", "보증"],
    "승무원": ["승무", "객실"], "호텔": ["호텔", "숙박", "관광"], "판매·MD": ["판매", "MD", "유통"],
    "외식·식음": ["조리", "급식", "영양", "식음"],
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
        "qual": _txt(raw.get("aplyQlfcCn"), 2000),
        "pref": _txt(raw.get("prefCondCn"), 1000),
        # 상세 화면용 (API에 없으면 빈 값 — 화면에서 해당 칸을 숨김)
        "acbg": (raw.get("acbgCondNmLst") or "").strip(),
        "steps": _txt(raw.get("scrnprcdrMthdExpln"), 1500),
        "disqual": _txt(raw.get("disqlfcRsn"), 1000),
        "replmpr": (raw.get("replmprYn") or "").strip() == "Y",
    }


def _txt(v, n):
    """공고 본문 칸: 줄바꿈은 살리고 연속 공백만 정리"""
    s = str(v or "").replace("\r", "")
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s).strip()
    return s[:n]


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


# ───────────── 채용공고 탐색 화면 (잡코리아식 필터) ─────────────
JOBS = list(JOB_TO_NCS.keys())
HIRE_GROUPS = ["정규직", "무기계약직", "비정규직", "청년인턴"]
SE_GROUPS = ["신입", "경력", "신입+경력"]
SORTS = ("match", "deadline", "recent", "nope")


# NCS 분류가 '정보통신' 하나로 겹치는 직군은 제목·자격요건 단어까지 맞아야 그 직군으로 센다
JOB_REQUIRE_KW = {
    "클라우드·인프라": ["클라우드", "인프라", "시스템", "서버", "네트워크", "전산", "데이터센터", "DB", "운영"],
    "정보보안": ["보안", "정보보호", "개인정보", "관제", "침해", "해킹"],
}


def _in_job(x, job):
    if not set(JOB_TO_NCS.get(job, [])).intersection(x["ncs"]):
        return False
    kws = JOB_REQUIRE_KW.get(job)
    if not kws:
        return True
    text = (x["title"] + " " + x.get("qual", "")).lower()
    return any(k.lower() in text for k in kws)


def _in_sub(x, sub):
    kws = SUB_KEYWORDS.get(sub, [])
    text = (x["title"] + " " + x.get("qual", "")).lower()
    return any(k.lower() in text for k in kws)


def _in_region(x, regions):
    if not regions:
        return True
    have = x["region"].split(",")
    return any(t in have for r in regions for t in _region_tokens(r))


def _in_hire(x, hires):
    if not hires:
        return True
    have = x["hire"].split(",")
    # "청년인턴(체험형)"·"청년인턴(채용형)"처럼 괄호가 붙어 오는 값도 묶어서 본다
    return any(any(h == v or v.startswith(h) for v in have) for h in hires)


def _in_se(x, ses):
    return not ses or x["se"] in ses


def _dday(x, today):
    end = _parse_ymd(x.get("end"))
    return (end - today).days if end else None


def browse(jobs=(), subs=(), regions=(), hires=(), ses=(), q="", closing=0, sort="match",
           page=1, size=20, career=""):
    """필터 + 정렬 + 페이지 + 항목별 개수(facet).
    facet 개수는 '그 항목만 빼고 나머지 필터를 걸었을 때' 개수 — 잡코리아처럼 눌렀을 때 몇 건 나올지 미리 보인다."""
    items, fetched_at, error, has_key = get_postings()
    today = date.today()
    qn = (q or "").strip().lower()
    jobs = [j for j in jobs if j in JOB_TO_NCS]
    subs = [s for s in subs if s in SUB_KEYWORDS]
    sort = sort if sort in SORTS else "match"

    def ok(x, skip=None):
        if skip != "job" and jobs and not any(_in_job(x, j) for j in jobs):
            return False
        if skip != "sub" and subs and not any(_in_sub(x, s) for s in subs):
            return False
        if skip != "region" and not _in_region(x, regions):
            return False
        if skip != "hire" and not _in_hire(x, hires):
            return False
        if skip != "se" and not _in_se(x, ses):
            return False
        if closing:
            d = _dday(x, today)
            if d is None or d > closing:
                return False
        if qn and qn not in (x["inst"] + " " + x["title"] + " " + x.get("qual", "")).lower():
            return False
        return True

    facets = {
        "job": {j: sum(1 for x in items if ok(x, "job") and _in_job(x, j)) for j in JOBS},
        "region": {r: sum(1 for x in items if ok(x, "region") and _in_region(x, [r])) for r in REGIONS},
        "hire": {h: sum(1 for x in items if ok(x, "hire") and _in_hire(x, [h])) for h in HIRE_GROUPS},
        "se": {e: sum(1 for x in items if ok(x, "se") and x["se"] == e) for e in SE_GROUPS},
    }
    # 세부 직무 개수는 고른 직군 안에서만 의미가 있다
    sub_pool = [x for x in items if ok(x, "sub")]
    facets["sub"] = {s: sum(1 for x in sub_pool if _in_sub(x, s)) for s in SUB_KEYWORDS}

    ncs_want = set(n for j in jobs for n in JOB_TO_NCS.get(j, []))
    rows = []
    for x in items:
        if not ok(x):
            continue
        d = _dday(x, today)
        s = 0
        s += 30 if any(_in_job(x, j) for j in jobs) else 0
        s += 10 if ncs_want and len(x["ncs"]) <= 2 else 0
        s += 25 if subs and any(_in_sub(x, sb) for sb in subs) else 0
        s += 6 if "정규직" in x["hire"].split(",") else 0
        if career == "신입" and x["se"] == "경력":
            s -= 40
        if career == "경력" and x["se"] == "신입":
            s -= 15
        sj, ss = _suggest_job(x["ncs"], jobs[0] if jobs else "", subs[0] if subs else "", ncs_want)
        rows.append((s, d, {**x, "dday": d, "suggest_job": sj, "suggest_sub": ss,
                            "jobs": [j for j in JOBS if _in_job(x, j)][:3]}))

    far = 9999
    if sort == "deadline":
        rows.sort(key=lambda t: (t[1] if t[1] is not None else far))
    elif sort == "recent":
        rows.sort(key=lambda t: str(t[2].get("start") or ""), reverse=True)
    elif sort == "nope":
        rows.sort(key=lambda t: -int(str(t[2].get("nope") or 0).strip() or 0) if str(t[2].get("nope") or "0").strip().isdigit() else 0)
    else:
        rows.sort(key=lambda t: (-t[0], t[1] if t[1] is not None else far))

    size = max(5, min(int(size or 20), 50))
    pages = max(1, (len(rows) + size - 1) // size)
    page = max(1, min(int(page or 1), pages))
    chunk = rows[(page - 1) * size: page * size]
    return {
        "items": [{k: v for k, v in t[2].items() if k not in ("disqual", "steps")} for t in chunk],
        "total": len(rows),
        "total_open": len(items),
        "page": page, "pages": pages, "size": size,
        "facets": facets,
        "options": {"jobs": JOBS, "regions": REGIONS,
                    "hires": HIRE_GROUPS, "ses": SE_GROUPS, "sorts": list(SORTS)},
        "fetched_at": datetime.utcfromtimestamp(fetched_at).strftime("%Y-%m-%dT%H:%M:%SZ") if fetched_at else None,
        "available": has_key and bool(items),
        "error": error if not items else None,
        "source": "잡알리오(재정경제부 공공기관 채용정보)",
    }


def detail(pid):
    """공고 한 건 전체 (자격요건·우대·전형절차 원문 포함)"""
    items, _, _, _ = get_postings()
    today = date.today()
    for x in items:
        if str(x.get("id")) == str(pid):
            sj, ss = _suggest_job(x["ncs"], "", "", set())
            return {**x, "dday": _dday(x, today), "suggest_job": sj, "suggest_sub": ss,
                    "jobs": [j for j in JOBS if _in_job(x, j)]}
    return None
