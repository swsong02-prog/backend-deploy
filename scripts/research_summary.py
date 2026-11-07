"""코치코치 효과 측정 집계 (2026-11-07)

서버 DB를 읽기 전용으로 열어 평균과 개수만 출력한다. 이름, 이메일, 답변 내용은 출력하지 않는다.

사용:
  python3 scripts/research_summary.py coachcoach.db
  python3 scripts/research_summary.py coachcoach.db --since 2026-11-10 --exclude test,demo
  python3 scripts/research_summary.py coachcoach.db --json      # 발표 자료용 숫자만 JSON으로

출력하는 숫자 (발표용 3개는 [발표] 표시)
  1. 긴장 풀기 루틴: 전후 긴장 점수 평균, 내려간 사람 비율          [발표]
  2. 첫 마디까지 걸린 시간: 같은 사람의 첫 면접과 최근 면접 비교     [발표]
  3. 습관어(분당): 같은 사람의 첫 면접과 최근 면접 비교             [발표]
  4. 참고: 말문 트기와 실전, 루틴을 한 면접과 안 한 면접 비교
"""
import argparse
import json
import sqlite3
import sys
from statistics import mean, median


def r1(x):
    return None if x is None else round(x, 1)


def safe_mean(xs):
    xs = [x for x in xs if x is not None]
    return mean(xs) if xs else None


def pct(part, whole):
    return None if not whole else round(100 * part / whole)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--since", default="", help="이 날짜(YYYY-MM-DD, UTC) 이후 기록만")
    ap.add_argument("--exclude", default="", help="이메일에 이 글자가 들어간 계정 제외(쉼표로 여러 개)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(question_results)")}
    has_calm = con.execute("SELECT 1 FROM sqlite_master WHERE name='calm_logs'").fetchone() is not None
    if "first_voice_ms" not in cols or not has_calm:
        print("이 DB에는 아직 효과 측정 칸이 없습니다. 11/07 서버를 배포한 뒤 실행하세요.", file=sys.stderr)
        sys.exit(1)

    excl = [x.strip().lower() for x in a.exclude.split(",") if x.strip()]
    users = {r["user_id"]: (r["email"] or "").lower() for r in con.execute("SELECT user_id, email FROM users")}
    ok_user = lambda uid: uid in users and not any(x in users[uid] for x in excl)
    since = a.since

    # ── 면접 단위 지표 ──
    sessions = []
    for s in con.execute("SELECT session_id, user_id, mode, created_at FROM interview_sessions ORDER BY created_at, session_id"):
        if not ok_user(s["user_id"]) or (since and (s["created_at"] or "") < since):
            continue
        rows = con.execute("SELECT duration_sec, filler_count, first_voice_ms FROM question_results WHERE session_id=?",
                           (s["session_id"],)).fetchall()
        dur = sum(r["duration_sec"] or 0 for r in rows)
        fill = sum(r["filler_count"] or 0 for r in rows)
        fv = [r["first_voice_ms"] for r in rows if r["first_voice_ms"] is not None]
        sessions.append({
            "id": s["session_id"], "user": s["user_id"], "mode": s["mode"] or "normal",
            "fv_sec": (median(fv) / 1000) if fv else None,          # 문항별 중앙값(한 문항이 튀어도 덜 흔들림)
            "filler_pm": (fill / dur * 60) if dur >= 30 else None,  # 30초 미만 면접은 분당 계산에서 뺌
        })

    calm = []
    for c in con.execute("SELECT user_id, session_id, pre, post, challenge, breath_sec, created_at FROM calm_logs"):
        if not ok_user(c["user_id"]) or (since and (c["created_at"] or "") < since):
            continue
        calm.append(dict(c))
    calm_sessions = {c["session_id"] for c in calm if c["session_id"]}

    # 1. 루틴 전후
    pairs = [c for c in calm if c["pre"] is not None and c["post"] is not None]
    diffs = [c["post"] - c["pre"] for c in pairs]
    breathed = [c["post"] - c["pre"] for c in pairs if (c["breath_sec"] or 0) >= 60]
    skipped = [c["post"] - c["pre"] for c in pairs if (c["breath_sec"] or 0) < 60]
    out_calm = {
        "루틴 완료 수": len(calm), "전후 점수 있는 수": len(pairs), "참여자 수": len({c["user_id"] for c in pairs}),
        "루틴 전 평균": r1(safe_mean(c["pre"] for c in pairs)), "루틴 후 평균": r1(safe_mean(c["post"] for c in pairs)),
        "평균 변화": r1(safe_mean(diffs)),
        "내려간 비율(%)": pct(sum(d < 0 for d in diffs), len(diffs)),
        "그대로(%)": pct(sum(d == 0 for d in diffs), len(diffs)),
        "올라간 비율(%)": pct(sum(d > 0 for d in diffs), len(diffs)),
        "호흡 1분 이상 한 경우 평균 변화": r1(safe_mean(breathed)), "호흡 건너뛴 경우 평균 변화": r1(safe_mean(skipped)),
        "위협(1)~도전(7) 평균": r1(safe_mean(c["challenge"] for c in calm)),
    }

    # 2, 3. 같은 사람의 첫 면접과 최근 면접
    def first_last(key):
        by_user = {}
        for s in sessions:
            if s[key] is not None:
                by_user.setdefault(s["user"], []).append(s[key])
        ps = [(v[0], v[-1]) for v in by_user.values() if len(v) >= 2]
        return {
            "비교 가능한 사람 수(면접 2회 이상)": len(ps),
            "첫 면접 평균": r1(safe_mean(p[0] for p in ps)), "최근 면접 평균": r1(safe_mean(p[1] for p in ps)),
            "줄어든 사람 비율(%)": pct(sum(p[1] < p[0] for p in ps), len(ps)),
        }

    # 4. 참고 비교 (같은 사람이 아니어서 인과로 말하면 안 됨)
    def group(pred, key):
        xs = [s[key] for s in sessions if pred(s) and s[key] is not None]
        return {"면접 수": len(xs), "평균": r1(safe_mean(xs))}

    out = {
        "기간": since or "전체", "제외한 계정 글자": excl, "면접 수": len(sessions),
        "면접 본 사람 수": len({s["user"] for s in sessions}),
        "[발표] 1. 긴장 풀기 루틴 전후(0~10점)": out_calm,
        "[발표] 2. 첫 마디까지 걸린 시간(초)": first_last("fv_sec"),
        "[발표] 3. 습관어(분당 횟수)": first_last("filler_pm"),
        "참고: 첫 마디(초) 말문 트기 / 실전": [group(lambda s: s["mode"] == "practice", "fv_sec"),
                                     group(lambda s: s["mode"] != "practice", "fv_sec")],
        "참고: 첫 마디(초) 루틴 한 면접 / 안 한 면접": [group(lambda s: s["id"] in calm_sessions, "fv_sec"),
                                        group(lambda s: s["id"] not in calm_sessions, "fv_sec")],
        "참고: 습관어(분당) 루틴 한 면접 / 안 한 면접": [group(lambda s: s["id"] in calm_sessions, "filler_pm"),
                                         group(lambda s: s["id"] not in calm_sessions, "filler_pm")],
    }

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    for k, v in out.items():
        if isinstance(v, dict):
            print(f"\n{k}")
            for kk, vv in v.items():
                print(f"  {kk}: {vv}")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"\n{k}")
            print(f"  {v[0]}  /  {v[1]}")
        else:
            print(f"{k}: {v}")
    print("\n주의: 참고 비교는 서로 다른 면접끼리 비교라서 '루틴 때문에 좋아졌다'는 인과 근거가 아닙니다.")


if __name__ == "__main__":
    main()
