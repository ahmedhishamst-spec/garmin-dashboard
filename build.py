#!/usr/bin/env python3
"""Build dashboard.html from data/*.json. One self-contained file: no server and
no internet needed (the chart library is embedded from vendor/chart.umd.js)."""
import json, os, sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ---- Tunable constants ------------------------------------------------------
DEEP_TARGET_MIN = 90                      # deep sleep minutes that score 100
WEIGHTS = {"hrv": 0.40, "deep": 0.35, "stress": 0.25}   # Recovery Score blend
INTENSITY_GOAL = 150                      # weekly intensity minutes goal
MIN_SESSION_MIN = 2                       # shorter activities are treated as noise
# -----------------------------------------------------------------------------


def load(name, default=None):
    try:
        with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def avg(vals, nd=1):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), nd) if v else None


def iso(d):
    return d.isoformat()


def pdate(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def chunk_parts(store):
    for ch in (store or {}).get("chunks", {}).values():
        for p in ch.get("parts", []):
            yield p.get("data")


# ---- Load -------------------------------------------------------------------
dailies = (load("dailies", {}) or {}).get("records", {})
sleep = (load("sleep", {}) or {}).get("records", {})
fitness = (load("fitnessage", {}) or {}).get("records", {})
acts = (load("activities", {}) or {}).get("activities", [])

hrv = {}
for d in chunk_parts(load("hrv", {})):
    if isinstance(d, dict):
        for s in d.get("hrvSummaries") or []:
            hrv[s["calendarDate"]] = s

weights = {}
for d in chunk_parts(load("weight", {})):
    if isinstance(d, dict):
        for s in d.get("dailyWeightSummaries") or []:
            w = (s.get("latestWeight") or {}).get("weight")
            if w:
                weights[s["summaryDate"]] = round(w / 1000.0 if w > 1000 else w, 1)

stress_alt = {}
for d in chunk_parts(load("stress", {})):
    if isinstance(d, list):
        for s in d:
            v = (s.get("values") or {}).get("overallStressLevel")
            if v is not None and v >= 0:
                stress_alt[s["calendarDate"]] = v

today = date.today()
dates = sorted(d for d in (set(dailies) | set(sleep) | set(k for k, v in hrv.items() if v.get("lastNightAvg") is not None))
               if pdate(d) <= today)
if not dates:
    sys.exit("No data found in data/. Run pull.py first.")

hrv_vals = [v["lastNightAvg"] for v in hrv.values() if v.get("lastNightAvg") is not None]
hrv_mean = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None


def stress_of(d):
    v = (dailies.get(d) or {}).get("averageStressLevel")
    if v is None or v < 0:
        v = stress_alt.get(d)
    return v if v is not None and v >= 0 else None


def recovery(d):
    comps = {}
    h = (hrv.get(d) or {}).get("lastNightAvg")
    if h is not None and hrv_mean:
        comps["hrv"] = clamp(65 + (h - hrv_mean) / hrv_mean * 200)
    sl = sleep.get(d)
    if sl and sl.get("deepSleepSeconds"):
        comps["deep"] = clamp(sl["deepSleepSeconds"] / 60.0 / DEEP_TARGET_MIN * 100)
    st = stress_of(iso(pdate(d) - timedelta(days=1)))
    if st is not None:
        comps["stress"] = clamp(100 - (st - 20) * 2.5)
    if len(comps) < 2:
        return None, comps
    wsum = sum(WEIGHTS[k] for k in comps)
    return round(sum(comps[k] * WEIGHTS[k] for k in comps) / wsum), dict((k, round(v)) for k, v in comps.items())


days = []
for d in dates:
    dd, sl, hv = dailies.get(d) or {}, sleep.get(d) or {}, hrv.get(d) or {}
    sc, comps = recovery(d)
    mins = lambda k: round(sl[k] / 60.0, 1) if sl.get(k) else None
    days.append({
        "d": d,
        "bmr": dd.get("bmrKilocalories"), "active": dd.get("activeKilocalories"),
        "steps": dd.get("totalSteps"), "mod": dd.get("moderateIntensityMinutes"),
        "vig": dd.get("vigorousIntensityMinutes"), "rhr": dd.get("restingHeartRate"),
        "stress": stress_of(d),
        "deep": mins("deepSleepSeconds"), "light": mins("lightSleepSeconds"),
        "rem": mins("remSleepSeconds"), "awake": mins("awakeSleepSeconds"),
        "sleepH": round(sl["sleepTimeSeconds"] / 3600.0, 2) if sl.get("sleepTimeSeconds") else None,
        "deepPct": round(sl["deepSleepSeconds"] * 100.0 / sl["sleepTimeSeconds"], 1)
        if sl.get("deepSleepSeconds") and sl.get("sleepTimeSeconds") else None,
        "sleepScore": ((sl.get("sleepScores") or {}).get("overall") or {}).get("value"),
        "hrv": hv.get("lastNightAvg"), "hrvWeekly": hv.get("weeklyAvg"),
        "bbHigh": dd.get("bodyBatteryHighestValue"), "bbLow": dd.get("bodyBatteryLowestValue"),
        "score": sc, "cHrv": comps.get("hrv"), "cDeep": comps.get("deep"), "cStress": comps.get("stress"),
    })

# Today is still in progress: its running totals would distort averages and charts.
for x in days:
    if x["d"] == iso(today):
        for k in ("bmr", "active", "steps", "mod", "vig", "stress"):
            x[k] = None

scored = [x for x in days if x["score"] is not None]
latest = scored[-1] if scored else None
last7 = scored[-7:]
prev7 = scored[-14:-7]

# ---- Fitness age ------------------------------------------------------------
fa_keys = sorted(k for k, v in fitness.items() if v.get("fitnessAge") is not None)
fa = None
if fa_keys:
    r = fitness[fa_keys[-1]]
    comp = r.get("components") or {}
    fa = {
        "date": fa_keys[-1], "fitness": round(r["fitnessAge"], 1),
        "chrono": r.get("chronologicalAge"), "best": round(r.get("achievableFitnessAge") or 0, 1) or None,
        "series": [{"d": k, "v": round(fitness[k]["fitnessAge"], 1)} for k in fa_keys],
        "rhr": (comp.get("rhr") or {}).get("value"),
        "vigMin": (comp.get("vigorousMinutesAvg") or {}).get("value"),
        "vigMinTarget": (comp.get("vigorousMinutesAvg") or {}).get("targetValue"),
        "vigDays": (comp.get("vigorousDaysAvg") or {}).get("value"),
        "vigDaysTarget": (comp.get("vigorousDaysAvg") or {}).get("targetValue"),
        "bmi": (comp.get("bmi") or {}).get("value"),
    }

# ---- Activities -------------------------------------------------------------
SPORTS = ["Calisthenics", "Padel", "Cardio", "Gym Strength"]


def sport(a):
    t = ((a.get("activityType") or {}).get("typeKey") or "").lower()
    n = (a.get("activityName") or "").lower()
    if any(x in t or x in n for x in ("padel", "tennis", "racket", "pickleball")):
        return "Padel"
    if t == "stop_watch" or "calisthenic" in n:
        return "Calisthenics"       # your rule: Timed Activity = home calisthenics
    if t in ("strength_training", "weight_training"):
        return "Gym Strength"
    return "Cardio"


sessions = []
for a in acts:
    mins = (a.get("duration") or 0) / 60.0
    sessions.append({
        "d": (a.get("startTimeLocal") or "")[:16], "name": a.get("activityName") or "",
        "type": (a.get("activityType") or {}).get("typeKey"), "sport": sport(a),
        "min": round(mins, 1), "km": round((a.get("distance") or 0) / 1000.0, 2),
        "hr": round(a["averageHR"]) if a.get("averageHR") else None,
        "maxhr": round(a["maxHR"]) if a.get("maxHR") else None,
        "kcal": round(a["calories"]) if a.get("calories") else None,
        "load": round(a["activityTrainingLoad"]) if a.get("activityTrainingLoad") else None,
        "mod": a.get("moderateIntensityMinutes"), "vig": a.get("vigorousIntensityMinutes"),
        "noise": mins < MIN_SESSION_MIN,
        "zones": [round((a.get("hrTimeInZone_%d" % i) or 0) / 60.0, 1) for i in range(1, 6)],
    })
sessions.sort(key=lambda s: s["d"], reverse=True)
real = [s for s in sessions if not s["noise"]]


def monday(d):
    return d - timedelta(days=d.weekday())


start_week = monday(pdate(dates[0]))
if real:
    start_week = min(start_week, monday(pdate(real[-1]["d"])))
weeks, w = [], start_week
while w <= monday(today):
    weeks.append(w)
    w += timedelta(days=7)
wk_train = dict((iso(w), dict((s, 0.0) for s in SPORTS)) for w in weeks)
for s in real:
    wk = iso(monday(pdate(s["d"])))
    if wk in wk_train:
        wk_train[wk][s["sport"]] += s["min"]
wk_int = dict((iso(w), [0, 0]) for w in weeks)
for x in days:
    wk = iso(monday(pdate(x["d"])))
    if wk in wk_int:
        wk_int[wk][0] += x["mod"] or 0
        wk_int[wk][1] += x["vig"] or 0
weekly = [{"w": k, "train": dict((s, round(v, 1)) for s, v in wk_train[k].items()),
           "mod": wk_int[k][0], "vig": wk_int[k][1]} for k in wk_train]

cut30 = iso(today - timedelta(days=30))
sport_cards = {}
for s in SPORTS:
    mine = [x for x in real if x["sport"] == s]
    m30 = [x for x in mine if x["d"][:10] >= cut30]
    sport_cards[s] = {"n30": len(m30), "min30": round(sum(x["min"] for x in m30)), "nAll": len(mine),
                      "minAll": round(sum(x["min"] for x in mine))}
zones = [round(sum(s["zones"][i] for s in real), 1) for i in range(5)]
last_act = real[0]["d"][:10] if real else None

# ---- KPIs and read-out ------------------------------------------------------
d7 = days[-7:]
d14p = days[-14:-7]
kpi = {
    "deep7": avg([x["deep"] for x in d7], 0), "hrv7": avg([x["hrv"] for x in d7], 0),
    "rhr": next((x["rhr"] for x in reversed(days) if x["rhr"]), None),
    "rhr7": avg([x["rhr"] for x in d7], 0),
    "stress7": avg([x["stress"] for x in d7], 0), "sleepH7": avg([x["sleepH"] for x in d7], 1),
    "sleepScore7": avg([x["sleepScore"] for x in d7], 0),
    "active7": avg([x["active"] for x in d7], 0), "activePrev": avg([x["active"] for x in d14p], 0),
    "bmr7": avg([x["bmr"] for x in d7], 0), "steps7": avg([x["steps"] for x in d7], 0),
    "hrvStatus": (hrv[max(hrv)] if hrv else {}).get("status"),
    "hrvBase": (hrv[max(hrv)] if hrv else {}).get("baseline"),
    "weight": ({"kg": weights[max(weights)], "d": max(weights),
                "delta": round(weights[max(weights)] - weights[min(weights)], 1) if len(weights) > 1 else None,
                "first": min(weights)} if weights else None),
    "weightSeries": [{"d": k, "v": v} for k, v in sorted(weights.items())],
    "thisWeekInt": (wk_int.get(iso(monday(today))) or [0, 0]),
}
kpi["thisWeekIntTotal"] = kpi["thisWeekInt"][0] + 2 * kpi["thisWeekInt"][1]

readout = []
if kpi["deep7"] is not None:
    readout.append("Deep sleep averaged %d min over the last 7 nights (target %d min)." % (kpi["deep7"], DEEP_TARGET_MIN))
if kpi["hrv7"] is not None and hrv_mean:
    diff = kpi["hrv7"] - hrv_mean
    readout.append("HRV 7-night average is %d ms, %s your period average of %d ms." % (
        kpi["hrv7"], "above" if diff > 1 else "below" if diff < -1 else "in line with", hrv_mean))
if kpi["active7"] is not None and kpi["activePrev"]:
    readout.append("Active calories: %d kcal/day this week vs %d the week before." % (kpi["active7"], kpi["activePrev"]))
readout.append("Intensity minutes this week: %d of %d (vigorous counts double)." % (kpi["thisWeekIntTotal"], INTENSITY_GOAL))
if last_act:
    gap = (today - pdate(last_act)).days
    readout.append("Last recorded session: %s (%d days ago)." % (last_act, gap))

payload = {
    "built": datetime.now().strftime("%Y-%m-%d %H:%M"), "today": iso(today),
    "first": dates[0], "last": dates[-1], "nDays": len(days),
    "days": days, "weekly": weekly, "sessions": sessions, "sports": sport_cards, "zones": zones,
    "score": latest, "score7": avg([x["score"] for x in last7], 0), "scorePrev7": avg([x["score"] for x in prev7], 0),
    "fa": fa, "kpi": kpi, "readout": readout, "hrvMean": round(hrv_mean, 1) if hrv_mean else None,
    "deepTarget": DEEP_TARGET_MIN, "intGoal": INTENSITY_GOAL,
    "weights": WEIGHTS, "sportsList": SPORTS,
}

# ---- Render -----------------------------------------------------------------
with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
    html = f.read()
with open(os.path.join(HERE, "vendor", "chart.umd.js"), encoding="utf-8") as f:
    chartjs = f.read().replace("</script", "<\\/script")
blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
html = html.replace("/*__CHARTJS__*/", chartjs).replace("/*__PAYLOAD__*/null", blob)
out = os.path.join(HERE, "dashboard.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("Built %s" % out)
print("Days: %d (%s to %s) | Activities: %d | Latest Recovery Score: %s" % (
    len(days), dates[0], dates[-1], len(real), latest["score"] if latest else "n/a"))
