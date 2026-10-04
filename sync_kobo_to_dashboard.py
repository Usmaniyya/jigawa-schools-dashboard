#!/usr/bin/env python3
"""
Pulls submissions from KoboToolbox and writes a single data.json file
that the public dashboard reads. Run this on a schedule (cron / GitHub
Actions) — it is the ONLY piece of the system that holds the API token.

Usage:
    export KOBO_API_TOKEN="your_token_here"
    export KOBO_ASSET_UID="aXXXXXXXXXXXXXXXXXXXXXX"
    python3 sync_kobo_to_dashboard.py

Output:
    data.json  — written next to this script, ready to copy beside
                 dashboard.html (or push to wherever the dashboard is hosted)
"""
import os
import sys
import json
from collections import defaultdict
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

KOBO_SERVER = os.environ.get("KOBO_SERVER", "https://kf.kobotoolbox.org")
API_TOKEN = os.environ.get("KOBO_API_TOKEN")
ASSET_UID = os.environ.get("KOBO_ASSET_UID")

if not API_TOKEN or not ASSET_UID:
    sys.exit(
        "Missing KOBO_API_TOKEN or KOBO_ASSET_UID environment variables.\n"
        "Set them before running this script (see the docstring above)."
    )


def fetch_submissions():
    """Pull all submissions for the deployed form, paging through results."""
    url = f"{KOBO_SERVER}/api/v2/assets/{ASSET_UID}/data.json?format=json&limit=2000"
    all_results = []
    while url:
        req = Request(url, headers={"Authorization": f"Token {API_TOKEN}"})
        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            sys.exit(f"Kobo API error {e.code}: {e.read().decode('utf-8', 'ignore')}")
        except URLError as e:
            sys.exit(f"Could not reach Kobo server: {e.reason}")
        all_results.extend(payload.get("results", []))
        url = payload.get("next")  # Kobo paginates; follow until exhausted
    return all_results


def num(rec, key, default=0):
    try:
        v = rec.get(key)
        return int(v) if v not in (None, "") else default
    except (ValueError, TypeError):
        return default


def pct(numerator, denominator):
    if not denominator:
        return 0
    return round(numerator / denominator * 100, 1)


def build_flags(rec):
    """Translate one submission's red-flag conditions into alert strings."""
    flags = []
    if rec.get("sec_b/attendance_band") == "below60":
        flags.append("Attendance below 60%")
    if rec.get("sec_h/school_bus_status") == "grounded":
        flags.append("School bus grounded")
    if rec.get("sec_f/fac_water_supply_functional") == "no":
        flags.append("No working water supply")
    if rec.get("sec_f/fac_toilets_condition") == "poor":
        flags.append("Toilets rated poor")
    if rec.get("snake_reptile_risk") == "yes":
        flags.append("Reptile/overgrown bush risk on premises")
    # Subjects with "NO TEACHER" written in remarks, inside the repeat group
    for entry in rec.get("sec_e_subjects", []) or []:
        remarks = (entry.get("subj_remarks") or "").upper()
        if "NO TEACHER" in remarks:
            subj = entry.get("subject_name", "a subject")
            flags.append(f"No teacher for {subj}")
    # Abandoned interventions
    for entry in rec.get("sec_g/sec_g_interventions", []) or []:
        if entry.get("intervention_status") == "abandoned":
            itype = entry.get("intervention_type", "a project")
            flags.append(f"{itype} intervention abandoned")
    return flags


def transform(records):
    schools = []
    lga_agg = defaultdict(lambda: {
        "schools": 0, "attendance_sum": 0, "attendance_n": 0,
        "neco_credit": 0, "neco_cand": 0,
        "facilities_functional": 0, "facilities_total": 0,
    })
    total_enrolment = 0
    flagged_count = 0

    for rec in records:
        name = rec.get("sec_a/school_name", "Unnamed school")
        lga = rec.get("sec_a/lga", "Unknown LGA")

        # Enrolment (calculate fields come back as strings)
        enrolment = num(rec, "sec_b/total_enrolment")
        total_enrolment += enrolment

        attendance_band = rec.get("sec_b/attendance_band")
        attendance_mid = {"above80": 90, "60to80": 70, "below60": 50}.get(attendance_band)

        # NECO 2026 success, if present
        cand = num(rec, "sec_k/neco_2026_cand_m") + num(rec, "sec_k/neco_2026_cand_f")
        credit = num(rec, "sec_k/neco_2026_credit_m") + num(rec, "sec_k/neco_2026_credit_f")

        # Facility functional ratio across the 16 key facilities
        facility_keys = [
            "classrooms", "science_labs", "library", "ict_room", "vocational_workshop",
            "water_supply", "toilets", "handwashing", "electricity", "security_lighting",
            "admin_block", "staff_accommodation", "perimeter_fence",
        ]
        fac_total, fac_yes = 0, 0
        for k in facility_keys:
            v = rec.get(f"sec_f/fac_{k}_functional")
            if v in ("yes", "no"):
                fac_total += 1
                if v == "yes":
                    fac_yes += 1

        flags = build_flags(rec)
        if flags:
            flagged_count += 1

        schools.append({
            "name": name, "lga": lga, "enrolment": enrolment,
            "attendance_band": attendance_band,
            "neco_pct": pct(credit, cand) if cand else None,
            "facilities_pct": pct(fac_yes, fac_total) if fac_total else None,
            "flags": flags,
        })

        agg = lga_agg[lga]
        agg["schools"] += 1
        if attendance_mid is not None:
            agg["attendance_sum"] += attendance_mid
            agg["attendance_n"] += 1
        agg["neco_credit"] += credit
        agg["neco_cand"] += cand
        agg["facilities_functional"] += fac_yes
        agg["facilities_total"] += fac_total

    lga_rows = []
    for lga, a in sorted(lga_agg.items()):
        lga_rows.append({
            "lga": lga,
            "schools": a["schools"],
            "avg_attendance": pct(a["attendance_sum"], a["attendance_n"] * 100) if a["attendance_n"] else None,
            "neco_pct": pct(a["neco_credit"], a["neco_cand"]) if a["neco_cand"] else None,
            "facilities_pct": pct(a["facilities_functional"], a["facilities_total"]) if a["facilities_total"] else None,
        })

    overall_neco = pct(
        sum(a["neco_credit"] for a in lga_agg.values()),
        sum(a["neco_cand"] for a in lga_agg.values()),
    )

    alerts = []
    for s in schools:
        for f in s["flags"]:
            alerts.append({"school": s["name"], "lga": s["lga"], "issue": f})

    return {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "kpis": {
            "schools_assessed": len(schools),
            "total_enrolment": total_enrolment,
            "avg_neco_pct": overall_neco,
            "flagged_schools": flagged_count,
        },
        "alerts": alerts,
        "lga_breakdown": lga_rows,
        "schools": schools,
    }


def main():
    print(f"Fetching submissions for asset {ASSET_UID} from {KOBO_SERVER} ...")
    records = fetch_submissions()
    print(f"Fetched {len(records)} submission(s).")
    data = transform(records)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
