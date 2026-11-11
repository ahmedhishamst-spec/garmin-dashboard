#!/usr/bin/env python3
"""Pull your Garmin history into data/*.json. Read-only. Standard library only.

  python3 pull.py            last 12 months (new data only on re-runs)
  python3 pull.py --days 90  a shorter window
  python3 pull.py --probe    only check which endpoints Garmin accepts

The API key is read from .env.local and is never printed or saved elsewhere."""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, timedelta

HOST = "https://personal.connectapi.garmin.com"
TOKEN_URL = HOST + "/personalapitoken-service/personal-api/personal-api-key/token"
ACTIVITIES = "/activitylist-service/activities/search/activities"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
THROTTLE = 0.25          # seconds between requests
PAGE = 20                # activities per page
RANGE_DAYS = 60          # maximum window for range endpoints

# Candidate endpoints per data type, tried in order. kind: "day" = one call per
# date, "range" = one call per window of up to 60 days.
CATS = {
    "dailies": [("day", "/usersummary-service/usersummary/daily", {"calendarDate": "{d}"}),
                ("range", "/usersummary-service/stats/steps/daily/{s}/{e}", {})],
    "stress": [("range", "/usersummary-service/stats/stress/daily/{s}/{e}", {}),
               ("day", "/wellness-service/wellness/dailyStress/{d}", {})],
    "bodybattery": [("range", "/wellness-service/wellness/bodyBattery/reports/daily",
                     {"startDate": "{s}", "endDate": "{e}"})],
    "intensity": [("range", "/usersummary-service/stats/im/daily/{s}/{e}", {})],
    "sleep": [("day", "/sleep-service/sleep/dailySleepData", {"date": "{d}"}),
              ("day", "/sleep-service/sleep/dailySleep", {"date": "{d}"})],
    "hrv": [("range", "/hrv-service/hrv/daily/{s}/{e}", {}),
            ("day", "/hrv-service/hrv/{d}", {})],
    "weight": [("range", "/weight-service/weight/range/{s}/{e}", {"includeAll": "true"}),
               ("range", "/weight-service/weight/range/{s}/{e}", {})],
    "fitnessage": [("day", "/fitnessage-service/fitnessage/{d}", {}),
                   ("day", "/fitnessage-service/fitnessage/v2/{d}", {})],
}
STEP = {"fitnessage": 7}   # changes slowly, sample weekly


def load_key():
    env = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()   # used by the cloud job
    if env:
        return env
    try:
        for line in open(os.path.join(HERE, ".env.local"), encoding="utf-8"):
            line = line.strip()
            if line.startswith("GARMIN_PERSONAL_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def fill(text, s="", e="", d=""):
    return text.replace("{s}", s).replace("{e}", e).replace("{d}", d)


def fill_params(params, s="", e="", d=""):
    return dict((k, fill(v, s, e, d)) for k, v in params.items())


def is_empty(o):
    if o is None:
        return True
    if isinstance(o, (list, dict, str)) and len(o) == 0:
        return True
    if isinstance(o, dict):
        # Garmin returns "shell" records for days without measurements
        if "sleepTimeSeconds" in o and not o.get("sleepTimeSeconds"):
            return True
        if "totalKilocalories" in o and not o.get("restingHeartRate") and not o.get("totalSteps"):
            return True
        if "chronologicalAge" in o and o.get("fitnessAge") is None:
            return True
        dto = o.get("dailySleepDTO")
        if isinstance(dto, dict) and not dto.get("sleepTimeSeconds"):
            return True
        return all(v is None or (isinstance(v, (list, dict, str)) and len(v) == 0) for v in o.values())
    return False


class Api:
    def __init__(self):
        self.key = load_key()
        self.token = None
        self.calls = 0
        if not self.key:
            sys.exit("API key is not set in .env.local")

    def refresh(self):
        req = urllib.request.Request(TOKEN_URL, data=b"", method="POST",
                                     headers={"Authorization": "ApiKey " + self.key,
                                              "User-Agent": "garmin-dashboard/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            sys.exit("Token request failed with HTTP %s. Is the key expired or revoked?" % ex.code)
        except Exception as ex:
            sys.exit("Token request failed: %s" % type(ex).__name__)
        try:
            data = json.loads(body)
            tok = data.get("access_token") or data.get("token") or data.get("accessToken")
        except ValueError:
            tok = body.strip()
        if not tok:
            sys.exit("Token response had an unexpected shape.")
        self.token = tok

    def get(self, path, params=None):
        """Returns (status, parsed_json_or_None). Retries 429 and 5xx with backoff."""
        url = HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
        if not self.token:
            self.refresh()
        refreshed, status = False, None
        for attempt in range(6):
            time.sleep(THROTTLE)
            self.calls += 1
            retry_after, body = None, ""
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token,
                                                       "Accept": "application/json",
                                                       "User-Agent": "garmin-dashboard/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    status, body = r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as ex:
                status, retry_after = ex.code, ex.headers.get("Retry-After")
            except Exception:
                status = None
            if status == 200:
                try:
                    return 200, (json.loads(body) if body.strip() else None)
                except ValueError:
                    return 200, None
            if status == 204:
                return 204, None
            if status == 401 and not refreshed:
                refreshed = True
                self.refresh()
                continue
            if status == 429:
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(5 * 2 ** attempt, 120)
                print("   rate limited (429), waiting %ss" % wait)
                time.sleep(wait)
                continue
            if status is None or 500 <= status <= 504:
                time.sleep(2 * 2 ** attempt)
                continue
            return status, None
        return status, None


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(DATA, name + ".json"))


def load(name, default):
    try:
        with open(os.path.join(DATA, name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def probe(api, kind, path, params):
    today = date.today()
    if kind == "range":
        s, e = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=1)).isoformat()
        return api.get(fill(path, s=s, e=e), fill_params(params, s=s, e=e))[0]
    d = (today - timedelta(days=2)).isoformat()
    return api.get(fill(path, d=d), fill_params(params, d=d))[0]


def resolve(api, cat):
    tried, working = [], []
    for kind, path, params in CATS[cat]:
        st = probe(api, kind, path, params)
        tried.append("%s -> %s" % (path.split("{")[0].rstrip("/"), st))
        if st in (200, 204):
            working.append((kind, path, params))
    return working, tried


def pull_activities(api, start):
    store = load("activities", {"by_id": {}})
    known = store["by_id"]
    complete = bool(store.get("complete"))
    store.pop("error", None)
    offset, reached = 0, False
    while True:
        st, obj = api.get(ACTIVITIES, {"start": offset, "limit": PAGE})
        if st != 200:
            store["error"] = "HTTP %s" % st
            break
        items = obj if isinstance(obj, list) else ((obj or {}).get("activities") or [])
        if not items:
            reached = True
            break
        new, oldest = 0, "9999"
        for a in items:
            aid = str(a.get("activityId"))
            if aid not in known:
                new += 1
            known[aid] = a
            oldest = min(oldest, (a.get("startTimeLocal") or a.get("startTimeGMT") or "9999")[:10])
        print("   activities at offset %d: %d items (oldest %s)" % (offset, len(items), oldest))
        if oldest < start.isoformat() or (complete and new == 0):
            reached = True
            break
        offset += len(items)   # Garmin may return fewer than requested per page
    if reached:
        store["complete"] = True
    store["activities"] = sorted(known.values(),
                                 key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    save("activities", store)
    return store


def fetch_window(api, path, params, qs, qe):
    """One window; if Garmin rejects it (400) or returns no content (204) for a
    long window, retry in 14-day parts."""
    st, obj = api.get(fill(path, s=qs.isoformat(), e=qe.isoformat()),
                      fill_params(params, s=qs.isoformat(), e=qe.isoformat()))
    if st == 200 and not is_empty(obj):
        return 200, [{"from": qs.isoformat(), "to": qe.isoformat(), "data": obj}]
    if st in (400, 200, 204) and (qe - qs).days > 14:
        parts, cur = [], qs
        while cur <= qe:
            pe = min(cur + timedelta(days=13), qe)
            st2, obj2 = api.get(fill(path, s=cur.isoformat(), e=pe.isoformat()),
                                fill_params(params, s=cur.isoformat(), e=pe.isoformat()))
            if st2 not in (200, 204):
                return st2, []
            parts.append({"from": cur.isoformat(), "to": pe.isoformat(), "data": obj2})
            cur = pe + timedelta(days=1)
        return 200, parts
    if st in (200, 204):
        return 200, [{"from": qs.isoformat(), "to": qe.isoformat(), "data": obj}]
    return st, []


def pull_range(api, cat, endpoint, start, end):
    kind, path, params = endpoint
    store = load(cat, {"chunks": {}})
    store["kind"], store["endpoint"] = "range", path
    store.pop("error", None)
    chunks = store.setdefault("chunks", {})
    today = date.today()
    o = (start.toordinal() // RANGE_DAYS) * RANGE_DAYS
    while o <= end.toordinal():
        cs, ce = date.fromordinal(o), date.fromordinal(o + RANGE_DAYS - 1)
        qs, qe = max(cs, start), min(ce, end)
        cached = chunks.get(cs.isoformat())
        if (cached is None or cached["from"] > qs.isoformat() or cached["to"] < qe.isoformat()
                or qe >= today - timedelta(days=3)):
            st, parts = fetch_window(api, path, params, qs, qe)
            if st != 200:
                store["error"] = "HTTP %s" % st
                break
            chunks[cs.isoformat()] = {"from": qs.isoformat(), "to": qe.isoformat(), "parts": parts}
        o += RANGE_DAYS
    save(cat, store)
    return store


def pull_days(api, cat, endpoint, start, end):
    kind, path, params = endpoint
    store = load(cat, {"records": {}, "empty": []})
    store["kind"], store["endpoint"] = "day", path
    store.pop("error", None)
    recs, empty = store.setdefault("records", {}), set(store.get("empty", []))
    for ds in [k for k, v in recs.items() if is_empty(v)]:   # clean old shells
        recs.pop(ds)
        empty.add(ds)
    step, today, d, n, fails = STEP.get(cat, 1), date.today(), start, 0, 0
    while d <= end:
        ds = d.isoformat()
        if (ds in recs or ds in empty) and (today - d).days > 3:
            d += timedelta(days=step)
            continue
        st, obj = api.get(fill(path, d=ds), fill_params(params, d=ds))
        if st in (200, 204):
            fails = 0
            if is_empty(obj):
                recs.pop(ds, None)
                empty.add(ds)
            else:
                recs[ds] = obj
                empty.discard(ds)
        else:
            fails += 1
            if fails >= 5:
                store["error"] = "HTTP %s" % st
                break
        n += 1
        if n % 40 == 0:
            print("   %s: %s ..." % (cat, ds))
            store["empty"] = sorted(empty)
            save(cat, store)
        d += timedelta(days=step)
    store["empty"] = sorted(empty)
    save(cat, store)
    return store


def count(store):
    if "records" in store:
        k = sorted(store["records"])
        return len(k), (k[0] if k else "-"), (k[-1] if k else "-")
    n, lo, hi = 0, "9999", "0000"
    for c in store.get("chunks", {}).values():
        for p in c.get("parts", []):
            data = p.get("data")
            items = data if isinstance(data, list) else ([data] if data else [])
            if items and not is_empty(data):
                n += len(items)
                lo, hi = min(lo, p["from"]), max(hi, p["to"])
    return n, (lo if n else "-"), (hi if n else "-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()
    end = date.today()
    start = end - timedelta(days=args.days)
    api = Api()
    print("Window: %s to %s (%d days)" % (start, end, args.days))
    resolved, skipped = {}, {}
    for cat in CATS:
        eps, tried = resolve(api, cat)
        if eps:
            resolved[cat] = eps
            print("[ok]      %-12s %s" % (cat, " | ".join(e[1].split("{")[0] for e in eps)))
        else:
            skipped[cat] = tried
            print("[missing] %-12s %s" % (cat, "; ".join(tried)))
    if args.probe:
        print("\nProbe only, nothing saved. Run without --probe to pull the data.")
        return
    print("\nActivities ...")
    act = pull_activities(api, start)
    dates = [(a.get("startTimeLocal") or "")[:10] for a in act["activities"]] or ["-"]
    summary = {"activities": (len(act["by_id"]), min(dates), max(dates))}
    errors = {}
    if act.get("error"):
        errors["activities"] = act["error"]
    used = {}
    for cat, eps in resolved.items():
        for ep in eps:
            print("\n%s ..." % cat)
            store = (pull_range if ep[0] == "range" else pull_days)(api, cat, ep, start, end)
            n = count(store)
            summary[cat] = n
            used[cat] = ep
            if store.get("error"):
                errors[cat] = store["error"]
            if n[0] > 0:
                break
            print("   no data from this endpoint, trying the next one" if ep is not eps[-1] else "   no data")
    save("_meta", {"pulled_on": end.isoformat(), "start": start.isoformat(),
                   "endpoints": dict((c, e[1]) for c, e in used.items()),
                   "unavailable": skipped})
    print("\n==== What came back ====")
    for cat, (n, lo, hi) in summary.items():
        print("%-12s %5d records   %s to %s" % (cat, n, lo, hi))
    for cat, tried in skipped.items():
        print("%-12s NOT AVAILABLE (%s)" % (cat, "; ".join(tried)))
    for cat, err in errors.items():
        print("%-12s stopped early: %s" % (cat, err))
    print("\n%d requests. Files are in %s" % (api.calls, DATA))


if __name__ == "__main__":
    main()
