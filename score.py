"""Phase 2 (Handshake): filter + rank discovered jobs into a daily shortlist.

Hard filters: job type in {internship, co-op}, not already applied, US-eligible
location, and not a clear work-auth exclusion (uses the structured `studentScreen`
field, not regex). Soft scoring: role/title keywords, CPT/OPT or sponsor friendliness,
recency. Merges all daily jobs_*.json so open unapplied jobs are re-offered.

Usage: python3 score.py [--date YYYY-MM-DD] [--all]
"""

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text())["score"]

US_MARKERS = {
    "united states", "usa", "u.s.", "north america", "remote",
    "california", "texas", "virginia", "maryland", "michigan", "florida",
    "new york", "washington", "colorado", "illinois", "georgia", "arizona",
    "massachusetts", "ohio", "pennsylvania", "north carolina", "new jersey",
    "san francisco", "bay area", "new york city", "washington dc", "washington, d.c.",
    "boston", "sunnyvale", "atlanta", "chicago", "los angeles", "palo alto",
    "miami", "seattle", "houston", "san diego", "austin", "denver", "dallas",
    "mountain view", "san jose", "philadelphia", "phoenix", "portland",
    "salt lake city", "pittsburgh", "raleigh", "durham", "boulder", "arlington",
    "minneapolis", "nashville", "charlotte", "columbus", "detroit", "baltimore",
    "santa clara", "menlo park", "redwood city", "oakland", "berkeley", "irvine",
    "cambridge", "brooklyn", "manhattan", "jersey city", "stamford", "tampa",
    # state abbreviations (match ", MA" style)
    ", ma", ", ca", ", ny", ", tx", ", va", ", wa", ", il", ", ga", ", nj", ", pa",
    ", co", ", fl", ", nc", ", oh", ", md", ", az", ", mi", ", mn", ", dc",
}
FOREIGN_MARKERS = {
    "india", "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad", "pune",
    "chennai", "noida", "gurgaon", "gurugram", "kolkata", "canada", "toronto",
    "ontario", "montreal", "vancouver", "united kingdom", "london", "europe",
    "germany", "berlin", "france", "paris", "spain", "madrid", "netherlands",
    "amsterdam", "china", "beijing", "shanghai", "japan", "tokyo", "singapore",
    "australia", "sydney", "dubai", "uae", "israel", "tel aviv", "brazil",
    "mexico", "ireland", "dublin", "switzerland", "zurich", "sweden", "stockholm",
}


def classify_location(job):
    toks = " | ".join(job.get("locations") or []).lower()
    if not toks:
        # No explicit location: remote flag or unknown.
        return "us" if job.get("remote") else "unsure"
    if any(m in toks for m in US_MARKERS):
        return "us"
    if any(m in toks for m in FOREIGN_MARKERS):
        return "foreign"
    return "unsure"


def classify_visa(job):
    """From Handshake's structured studentScreen. Dev is F-1 (CPT/OPT), needs no
    sponsorship. -> 'cpt_opt' | 'sponsors' | 'no' | 'unknown'."""
    ss = job.get("studentScreen") or {}
    if ss.get("acceptsCptCandidates") or ss.get("acceptsOptCandidates") or ss.get("acceptsOptCptCandidates"):
        return "cpt_opt"
    if ss.get("willingToSponsorCandidate"):
        return "sponsors"
    # Requires work auth, doesn't accept CPT/OPT and won't sponsor -> excludes an F-1 student.
    if ss.get("workAuthRequired") and not ss.get("workAuthNotDisclosed"):
        return "no"
    return "unknown"


def _job_age_days(job, today):
    for k in ("applyStart", "createdAt"):
        v = job.get(k)
        if not v:
            continue
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00")).date()
            return (today - d).days
        except Exception:
            continue
    return None


def score_job(job, today):
    w = CFG["weights"]
    score, reasons = 0, []
    title = (job.get("title") or "").lower()
    for pat, pts in CFG["title_keywords_bonus"].items():
        if re.search(pat, title):
            score += pts
            reasons.append(f"kw:{pat}+{pts}")

    visa = classify_visa(job)
    if visa == "cpt_opt":
        score += w["cpt_opt_accepted"]; reasons.append("visa:cpt_opt")
    elif visa == "sponsors":
        score += w["sponsors"]; reasons.append("visa:sponsors")
    else:
        reasons.append(f"visa:{visa}")

    age = _job_age_days(job, today)
    if age is not None:
        if age <= 2:
            score += w["posted_within_2d"]; reasons.append(f"fresh:{age}d")
        elif age <= 7:
            score += w["posted_within_7d"]; reasons.append(f"fresh:{age}d")
        elif age <= 30:
            score += w["posted_within_30d"]
    if job.get("remote"):
        score += w["remote_us"]
    return score, visa, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    jobs = {}
    for f in sorted(DATA.glob("jobs_*.json")):
        try:
            jobs.update(json.loads(f.read_text()))
        except Exception:
            pass
    applied_path = DATA / "applied.json"
    applied = json.loads(applied_path.read_text()) if applied_path.exists() else {}
    today = date.today()
    want_types = set(CFG["job_types"])

    kept, dropped = [], {"wrong_type": 0, "already_applied": 0, "foreign": 0, "visa_no": 0}
    for jid, j in jobs.items():
        if (j.get("type") or "").lower() not in want_types:
            dropped["wrong_type"] += 1
            continue
        if jid in applied:
            dropped["already_applied"] += 1
            continue
        loc = classify_location(j)
        if loc == "foreign":
            dropped["foreign"] += 1
            continue
        score, visa, reasons = score_job(j, today)
        if visa == "no":
            dropped["visa_no"] += 1
            continue
        if loc == "unsure":
            score += CFG["weights"]["location_unsure"]
            reasons.append("loc:unsure")
        kept.append({**j, "score": score, "visa": visa, "locationClass": loc, "scoreReasons": reasons})

    kept.sort(key=lambda j: -j["score"])
    shortlist = kept[: CFG["shortlist_size"]]
    out = DATA / f"shortlist_{args.date}.json"
    out.write_text(json.dumps(shortlist, indent=1))

    print(f"input={len(jobs)} dropped={dropped} ranked={len(kept)} -> {out.name} (top {len(shortlist)})")
    for j in (kept if args.all else shortlist):
        print(f"{j['score']:>4}  {(j.get('title') or '')[:50]:<50} {(j.get('company') or '')[:24]:<24} "
              f"{(j.get('type') or '')[:10]:<10} visa={j['visa']:<8} {(j.get('locations') or ['—'])[0][:24]}")


if __name__ == "__main__":
    main()
