#!/usr/bin/env python3
"""
Pull your own Garmin Connect data (activities + daily wellness) and save it
as plain-English markdown notes + a data.json file that an AI assistant can
read. Read-only: this script never writes anything back to Garmin.

Usage:
    python sync_garmin.py --login                  # one-time login (only time you type a password)
    python sync_garmin.py --days 3 --dry-run        # test: print last 3 days, write nothing
    python sync_garmin.py --days 3                  # pull last 3 days into ./garmin
    python sync_garmin.py --export-ci-token         # bundle the saved login for GitHub Actions use
"""

import argparse
import base64
import contextlib
import getpass
import io
import json
import os
import re
import sys
import zipfile
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_DIR = os.path.join(SCRIPT_DIR, ".garmin_tokens")
CI_TOKEN_FILE = os.path.join(SCRIPT_DIR, "garmin-ci-token.txt")


def _dig(d, *paths, default=None):
    """Try several dotted key-paths against nested dicts/lists; return the first hit."""
    for path in paths:
        cur = d
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def get_garmin_module():
    try:
        import garminconnect
    except ImportError:
        sys.exit(
            "The garminconnect library isn't installed.\n"
            "Run: pip install -r requirements.txt"
        )
    return garminconnect


def do_login():
    """One-time interactive login. Prompts for email/password/2FA, saves a
    reusable token to disk, and never prints or stores the raw password."""
    garminconnect = get_garmin_module()
    Garmin = garminconnect.Garmin

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (hidden, won't show as you type): ")

    def prompt_mfa():
        return input("Enter the one-time code Garmin just sent you: ").strip()

    garmin = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)

    os.makedirs(TOKEN_DIR, exist_ok=True)
    try:
        # Passing tokenstore here makes the library save the session to disk
        # itself once login (including any MFA step) succeeds.
        garmin.login(TOKEN_DIR)
    except Exception as e:
        sys.exit(f"Login failed: {e}")

    print(f"\nLogin saved. Token stored locally at:\n  {TOKEN_DIR}")
    print("You won't need to log in again until that expires (about a year).")


def get_client():
    """Load a client from the saved token, without ever asking for a password."""
    garminconnect = get_garmin_module()
    Garmin = garminconnect.Garmin

    if not os.path.isdir(TOKEN_DIR) or not os.listdir(TOKEN_DIR):
        sys.exit(
            "No saved Garmin login found.\n"
            "Run: python sync_garmin.py --login"
        )

    garmin = Garmin()
    try:
        garmin.login(TOKEN_DIR)
    except Exception as e:
        sys.exit(
            f"Saved login didn't work ({e}).\n"
            "It may have expired. Run: python sync_garmin.py --login"
        )
    # A loaded token that's near expiry gets silently refreshed (and Garmin
    # rotates the refresh token when that happens) without the library
    # persisting it back to TOKEN_DIR on this code path. Always re-dump so a
    # rotated token doesn't get discarded and brick the next run.
    with contextlib.suppress(Exception):
        garmin.client.dump(TOKEN_DIR)
    return garmin


def export_ci_token():
    if not os.path.isdir(TOKEN_DIR) or not os.listdir(TOKEN_DIR):
        sys.exit("No saved login yet. Run --login first.")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(TOKEN_DIR):
            zf.write(os.path.join(TOKEN_DIR, fname), arcname=fname)

    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    with open(CI_TOKEN_FILE, "w") as f:
        f.write(encoded)
    print(f"Wrote {CI_TOKEN_FILE}")
    print("Paste its contents into your GARMIN_TOKEN_B64 GitHub secret, then delete this file.")


def load_ci_token_into_tokendir():
    """For CI use: rebuild the token directory from the GARMIN_TOKEN_B64 env var."""
    b64 = os.environ.get("GARMIN_TOKEN_B64")
    if not b64:
        return
    os.makedirs(TOKEN_DIR, exist_ok=True)
    data = base64.b64decode(b64)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(TOKEN_DIR)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_wellness_for_day(garmin, day):
    d = day.isoformat()
    entry = {"date": d}

    try:
        stats = garmin.get_stats(d)
    except Exception:
        stats = {}
    entry["resting_hr"] = _dig(stats, "restingHeartRate")
    entry["steps"] = _dig(stats, "totalSteps")
    entry["stress_avg"] = _dig(stats, "averageStressLevel")

    try:
        sleep = garmin.get_sleep_data(d)
    except Exception:
        sleep = {}
    dto = _dig(sleep, "dailySleepDTO", default={}) or {}
    sleep_seconds = _dig(dto, "sleepTimeSeconds") or _dig(sleep, "sleepTimeSeconds")
    entry["sleep_hours"] = round(sleep_seconds / 3600, 1) if sleep_seconds else None
    entry["sleep_score"] = _dig(dto, "sleepScores.overall.value") or _dig(dto, "sleepScores.overallScore")

    try:
        hrv = garmin.get_hrv_data(d)
    except Exception:
        hrv = {}
    entry["hrv_overnight"] = _dig(
        hrv, "hrvSummary.lastNightAvg", "hrvSummary.weeklyAvg", "lastNightAvg"
    )

    try:
        bb = garmin.get_body_battery(d, d)
    except Exception:
        bb = None
    bb_start = bb_end = None
    if isinstance(bb, list) and bb:
        arr = _dig(bb[0], "bodyBatteryValuesArray") or []
        values = [v[1] for v in arr if isinstance(v, (list, tuple)) and len(v) > 1 and v[1] is not None]
        if values:
            bb_start, bb_end = values[0], values[-1]
    entry["body_battery_start"] = bb_start
    entry["body_battery_end"] = bb_end

    try:
        readiness = garmin.get_training_readiness(d)
    except Exception:
        readiness = None
    if isinstance(readiness, list) and readiness:
        readiness = readiness[0]
    entry["training_readiness"] = _dig(readiness or {}, "score")
    entry["training_readiness_level"] = _dig(readiness or {}, "level")
    entry["training_readiness_feedback"] = _dig(readiness or {}, "feedbackShort")

    return entry


def fetch_activities(garmin, days):
    try:
        activities = garmin.get_activities(0, 50)
    except Exception:
        activities = []
    cutoff = date.today() - timedelta(days=days)
    out = []
    for a in activities:
        start = _dig(a, "startTimeLocal", default="")
        try:
            a_date = date.fromisoformat(start[:10])
        except ValueError:
            continue
        if a_date < cutoff:
            continue
        avg_speed = a.get("averageSpeed")
        max_speed = a.get("maxSpeed")
        cadence = _dig(
            a,
            "averageBikingCadenceInRevPerMinute",
            "averageRunningCadenceInStepsPerMinute",
            "averageSwimCadenceInStrokesPerMinute",
        )
        hr_zones = [a.get(f"hrTimeInZone_{i}") for i in range(1, 6)]
        out.append({
            "id": a.get("activityId"),
            "name": a.get("activityName"),
            "type": _dig(a, "activityType.typeKey"),
            "date": start[:10],
            "start_time": start,
            "duration_min": round(a["duration"] / 60, 1) if a.get("duration") else None,
            "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else None,
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "calories": a.get("calories"),
            "avg_pace_min_per_km": None,
            "elevation_gain_m": a.get("elevationGain"),
            "elevation_loss_m": a.get("elevationLoss"),
            "min_elevation_m": a.get("minElevation"),
            "max_elevation_m": a.get("maxElevation"),
            "avg_speed_kmh": round(avg_speed * 3.6, 1) if avg_speed else None,
            "max_speed_kmh": round(max_speed * 3.6, 1) if max_speed else None,
            "avg_cadence_rpm": cadence,
            "aerobic_training_effect": round(a["aerobicTrainingEffect"], 1) if a.get("aerobicTrainingEffect") is not None else None,
            "anaerobic_training_effect": round(a["anaerobicTrainingEffect"], 1) if a.get("anaerobicTrainingEffect") is not None else None,
            "training_effect_label": a.get("trainingEffectLabel"),
            "training_load": round(a["activityTrainingLoad"]) if a.get("activityTrainingLoad") is not None else None,
            "location_name": a.get("locationName"),
            "min_temp_c": a.get("minTemperature"),
            "max_temp_c": a.get("maxTemperature"),
            "hr_zone_minutes": [round(z / 60, 1) if z is not None else None for z in hr_zones],
        })
    return out


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def fmt(value, suffix=""):
    return f"{value}{suffix}" if value is not None else "no data"


def render_daily_md(entry):
    lines = [f"# Garmin wellness {entry['date']}"]
    lines.append(f"- Resting HR: {fmt(entry.get('resting_hr'), ' bpm')}")
    lines.append(f"- HRV (overnight): {fmt(entry.get('hrv_overnight'), ' ms')}")
    sleep_bit = fmt(entry.get('sleep_hours'), ' h')
    if entry.get("sleep_score") is not None:
        sleep_bit += f" (score {entry['sleep_score']})"
    lines.append(f"- Sleep: {sleep_bit}")
    bb_start, bb_end = entry.get("body_battery_start"), entry.get("body_battery_end")
    if bb_start is not None and bb_end is not None:
        lines.append(f"- Body battery: {bb_start} -> {bb_end}")
    else:
        lines.append("- Body battery: no data")
    lines.append(f"- Stress (avg): {fmt(entry.get('stress_avg'))}")
    lines.append(f"- Steps: {fmt(entry.get('steps'))}")
    tr = entry.get("training_readiness")
    if tr is not None:
        tr_text = f"{tr}/100"
        extras = [e for e in (entry.get("training_readiness_level"),) if e]
        feedback = entry.get("training_readiness_feedback")
        if feedback:
            extras.append(feedback.replace("_", " ").lower())
        if extras:
            tr_text += f" ({' - '.join(extras)})"
    else:
        tr_text = "no data"
    lines.append(f"- Training readiness: {tr_text}")
    return "\n".join(lines) + "\n"


def slugify(text):
    text = (text or "activity").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


def render_activity_md(a):
    lines = [f"# {a.get('name') or 'Activity'} ({a.get('type') or 'unknown type'})"]
    if a.get("location_name"):
        lines.append(f"- Location: {a['location_name']}")
    lines.append(f"- Date: {a.get('date')} at {a.get('start_time', '')[11:16]}")
    lines.append(f"- Duration: {fmt(a.get('duration_min'), ' min')}")
    lines.append(f"- Distance: {fmt(a.get('distance_km'), ' km')}")
    lines.append(f"- Avg speed: {fmt(a.get('avg_speed_kmh'), ' km/h')} (max {fmt(a.get('max_speed_kmh'), ' km/h')})")
    lines.append(f"- Elevation gain: {fmt(a.get('elevation_gain_m'), ' m')} (loss {fmt(a.get('elevation_loss_m'), ' m')})")
    lines.append(f"- Avg HR: {fmt(a.get('avg_hr'), ' bpm')} (max {fmt(a.get('max_hr'), ' bpm')})")
    lines.append(f"- Avg cadence: {fmt(a.get('avg_cadence_rpm'), ' rpm')}")
    lines.append(f"- Calories: {fmt(a.get('calories'))}")
    if a.get("training_effect_label"):
        lines.append(
            f"- Training effect: {a['training_effect_label'].replace('_', ' ').title()} "
            f"(aerobic {fmt(a.get('aerobic_training_effect'))}, anaerobic {fmt(a.get('anaerobic_training_effect'))}, "
            f"load {fmt(a.get('training_load'))})"
        )
    if a.get("min_temp_c") is not None:
        lines.append(f"- Temperature: {a['min_temp_c']}-{a.get('max_temp_c')} C")
    zones = a.get("hr_zone_minutes")
    if zones and any(z is not None for z in zones):
        zone_str = ", ".join(f"Z{i+1} {fmt(z, 'm')}" for i, z in enumerate(zones))
        lines.append(f"- HR zones: {zone_str}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

def write_files_sink(out_dir, wellness_entries, activities):
    daily_dir = os.path.join(out_dir, "daily")
    activities_dir = os.path.join(out_dir, "activities")
    os.makedirs(daily_dir, exist_ok=True)
    os.makedirs(activities_dir, exist_ok=True)

    data_path = os.path.join(out_dir, "data.json")
    if os.path.exists(data_path):
        with open(data_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    else:
        store = {"wellness": {}, "activities": {}}

    for entry in wellness_entries:
        store["wellness"][entry["date"]] = entry
        with open(os.path.join(daily_dir, f"{entry['date']}.md"), "w", encoding="utf-8") as f:
            f.write(render_daily_md(entry))

    for a in activities:
        store["activities"][str(a["id"])] = a
        fname = f"{a['date']}-{a['id']}-{slugify(a.get('name'))}.md"
        with open(os.path.join(activities_dir, fname), "w", encoding="utf-8") as f:
            f.write(render_activity_md(a))

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, default=str)

    print(f"Wrote {len(wellness_entries)} daily note(s) and {len(activities)} activity note(s) to {out_dir}")


def write_supabase_sink(wellness_entries, activities):
    import urllib.request

    url = os.environ.get("GARMIN_INGEST_URL")
    secret = os.environ.get("GARMIN_INGEST_SECRET")
    if not url:
        sys.exit("GARMIN_INGEST_URL is not set.")

    payload = json.dumps(
        {"activities": activities, "wellness": wellness_entries}, default=str
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Posted to {url}: HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true", help="One-time interactive login")
    parser.add_argument("--export-ci-token", action="store_true", help="Bundle saved login for GitHub Actions")
    parser.add_argument("--days", type=int, default=3, help="How many recent days to pull (default 3)")
    parser.add_argument("--dry-run", action="store_true", help="Print results, write nothing")
    parser.add_argument("--sink", choices=["files", "supabase"], default="files")
    parser.add_argument("--out", default=os.path.join(SCRIPT_DIR, "garmin"), help="Output folder for --sink files")
    args = parser.parse_args()

    if args.login:
        do_login()
        return

    if args.export_ci_token:
        export_ci_token()
        return

    load_ci_token_into_tokendir()
    garmin = get_client()

    wellness_entries = []
    for i in range(args.days):
        day = date.today() - timedelta(days=i)
        print(f"Fetching wellness for {day.isoformat()}...")
        wellness_entries.append(fetch_wellness_for_day(garmin, day))

    print("Fetching recent activities...")
    activities = fetch_activities(garmin, args.days)

    if args.dry_run:
        print("\n--- DRY RUN: nothing written ---\n")
        for entry in wellness_entries:
            print(render_daily_md(entry))
        for a in activities:
            print(render_activity_md(a))
        return

    if args.sink == "files":
        write_files_sink(args.out, wellness_entries, activities)
    else:
        write_supabase_sink(wellness_entries, activities)


if __name__ == "__main__":
    main()
