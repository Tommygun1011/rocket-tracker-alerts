#!/usr/bin/env python3
"""
Rocket Tracker launch alerts for Discord.

Runs on a timer (GitHub Actions, every 15 minutes). Each run it:
  1. Gets upcoming launches from Launch Library 2 (The Space Devs).
  2. Posts an alert in #launch-alerts about 24 hours, 1 hour and
     15 minutes before each launch, pinging the matching roles.
  3. Remembers what it has already sent in alerts_state.json so it
     never posts the same alert twice. If a launch is delayed by more
     than 30 minutes, the alerts start again with the new time.

Only uses Python's built-in modules, so there is nothing to install.

Settings you might want to change are in the SETTINGS section below.
The Discord webhook link is NOT stored here. It comes from the
DISCORD_WEBHOOK_URL secret in GitHub so nobody can see it.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- SETTINGS

SITE_URL = "https://rockettracker.com.au"

# Discord role IDs for the Rocket Tracker server.
ROLES = {
    "all": "1558739904819896410",        # All Launches
    "spacex": "1558739927959601202",     # SpaceX
    "starship": "1558740060092760084",   # Starship
    "rocketlab": "1558740084629577850",  # Rocket Lab
    "nasa": "1558740109514383360",       # NASA
    "aussie": "1558740133254008862",     # Aussie Launches
}

# When to post. "minutes" = how long before launch the alert is due.
# Set "ping" to False for any alert you want posted WITHOUT pinging roles.
STAGES = [
    {"key": "day",   "minutes": 24 * 60, "ping": False, "label": "T-24 hours", "colour": 0x3B82F6},
    {"key": "hour",  "minutes": 60,      "ping": True,  "label": "T-1 hour",   "colour": 0xF59E0B},
    {"key": "final", "minutes": 15,      "ping": True,  "label": "Launching soon", "colour": 0xEF4444},
]

# A launch that moves by more than this many minutes gets its alerts reset.
RESCHEDULE_MINUTES = 30

# Most messages to send in one run (stops a big burst on the first run).
MAX_POSTS_PER_RUN = 6

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts_state.json")
API_URL = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/"
USER_AGENT = "RocketTrackerAlerts/1.0 (+https://rockettracker.com.au)"

# Launch Library status IDs: 1 = Go for Launch, 8 = To Be Confirmed.
ALERT_STATUSES = {1, 8}
# Time precision IDs: 0 = second, 1 = minute, 2 = hour.
PRECISE = {0, 1}
HOUR_OR_BETTER = {0, 1, 2}

# ------------------------------------------------------------------ HELPERS


def log(msg):
    print(msg, flush=True)


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_launches(now):
    """Launches from 1 hour ago up to 26 hours ahead."""
    params = (
        f"?limit=60&mode=normal&ordering=net"
        f"&net__gte={(now - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&net__lte={(now + timedelta(hours=26)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    headers = {}
    api_key = os.environ.get("LL2_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Token {api_key}"
    return get_json(API_URL + params, headers).get("results", [])


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def safe_get(obj, *keys, default=None):
    for key in keys:
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return default
        if obj is None:
            return default
    return obj


def matching_roles(launch):
    """Which roles to ping for this launch."""
    roles = ["all"]
    provider = safe_get(launch, "launch_service_provider", default={}) or {}
    provider_name = (provider.get("name") or "").lower()
    provider_id = provider.get("id")
    rocket = (safe_get(launch, "rocket", "configuration", "name", default="") or "").lower()
    rocket_full = (safe_get(launch, "rocket", "configuration", "full_name", default="") or "").lower()
    agencies = safe_get(launch, "mission", "agencies", default=[]) or []

    if provider_id == 121 or "spacex" in provider_name:
        roles.append("spacex")
    if "starship" in rocket or "starship" in rocket_full:
        roles.append("starship")
    if provider_id == 147 or "rocket lab" in provider_name:
        roles.append("rocketlab")
    if provider_id == 44 or any(a.get("id") == 44 or a.get("abbrev") == "NASA" for a in agencies):
        roles.append("nasa")

    pad_country = (safe_get(launch, "pad", "country", "alpha_2_code", default="")
                   or safe_get(launch, "pad", "location", "country", "alpha_2_code", default=""))
    aussie_payload = any(
        c.get("alpha_2_code") == "AU"
        for a in agencies
        for c in (a.get("country") or [])
        if isinstance(c, dict)
    )
    if pad_country == "AU" or aussie_payload:
        roles.append("aussie")
    return roles


def webcast_link(launch):
    vids = safe_get(launch, "mission", "vid_urls", default=[]) or launch.get("vid_urls") or []
    vids = [v for v in vids if isinstance(v, dict) and v.get("url")]
    if not vids:
        return None
    vids.sort(key=lambda v: v.get("priority") if isinstance(v.get("priority"), int) else 999)
    return vids[0]["url"]


def image_url(launch):
    img = launch.get("image")
    if isinstance(img, dict):
        return img.get("image_url") or img.get("thumbnail_url")
    if isinstance(img, str):
        return img
    return None


def short(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_message(launch, stage, net, rescheduled, test=False):
    unix = int(net.timestamp())
    rocket = safe_get(launch, "rocket", "configuration", "full_name") or safe_get(
        launch, "rocket", "configuration", "name", default="Unknown rocket")
    provider = safe_get(launch, "launch_service_provider", "name", default="Unknown")
    mission = safe_get(launch, "mission", "name") or launch.get("name", "Launch")
    pad = safe_get(launch, "pad", "name", default="Unknown pad")
    location = safe_get(launch, "pad", "location", "name", default="")
    orbit = safe_get(launch, "mission", "orbit", "name")
    status_id = safe_get(launch, "status", "id")
    status_name = safe_get(launch, "status", "name", default="")
    webcast = webcast_link(launch)

    role_keys = matching_roles(launch)
    role_ids = [ROLES[k] for k in role_keys if ROLES.get(k)]
    ping = stage["ping"] and not test
    mentions = " ".join(f"<@&{rid}>" for rid in role_ids) if ping else ""

    if stage["key"] == "final":
        headline = f"🔴 **{mission}** lifts off <t:{unix}:R>! Get to the stream."
    elif stage["key"] == "hour":
        headline = f"⏰ **{mission}** launches <t:{unix}:R>."
    else:
        headline = f"🗓️ **{mission}** is scheduled to launch <t:{unix}:R>."
    if rescheduled:
        headline += " (new time)"
    if test:
        headline = "🧪 **Test alert.** This is what launch alerts will look like:\n" + headline

    fields = [
        {"name": "🚀 Rocket", "value": short(rocket, 100), "inline": True},
        {"name": "🏢 Provider", "value": short(provider, 100), "inline": True},
        {"name": "🕒 Launch time", "value": f"<t:{unix}:F>\n<t:{unix}:R>", "inline": True},
        {"name": "📍 Launch site", "value": short(f"{pad}\n{location}".strip(), 200), "inline": True},
    ]
    if orbit:
        fields.append({"name": "🛰️ Orbit", "value": short(orbit, 100), "inline": True})
    status_text = status_name or "Unknown"
    if status_id == 8:
        status_text += " (time not yet confirmed)"
    fields.append({"name": "✅ Status", "value": short(status_text, 100), "inline": True})
    links = [f"[Live board on Rocket Tracker]({SITE_URL})"]
    if webcast:
        links.append(f"[Watch the webcast]({webcast})")
    fields.append({"name": "🔗 Links", "value": " • ".join(links), "inline": False})

    embed = {
        "author": {"name": stage["label"]},
        "title": short(launch.get("name", mission), 250),
        "url": SITE_URL,
        "description": short(safe_get(launch, "mission", "description", default=""), 350),
        "color": stage["colour"],
        "fields": fields,
        "footer": {"text": "Rocket Tracker • Launch data: The Space Devs"},
        "timestamp": net.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    img = image_url(launch)
    if img:
        embed["image"] = {"url": img}

    return {
        "username": "Rocket Tracker",
        "content": f"{mentions}\n{headline}".strip(),
        "embeds": [embed],
        # Only ever ping the roles we chose. Never @everyone, never users.
        "allowed_mentions": {"parse": [], "roles": role_ids if ping else []},
    }


def post_to_discord(webhook_url, payload):
    url = webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true"
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://rockettracker.com.au, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as err:
            if err.code == 429:
                try:
                    wait = float(json.load(err).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                log(f"Discord rate limit, waiting {wait:.1f}s")
                time.sleep(min(wait, 30) + 0.5)
                continue
            body = err.read().decode("utf-8", "replace")[:300]
            log(f"Discord rejected the message ({err.code}): {body}")
            return False
    return False


def due_stage(minutes_left):
    """The most urgent alert this launch is due for, or None."""
    if minutes_left <= 0:
        return None
    for stage in reversed(STAGES):  # final first
        if minutes_left <= stage["minutes"]:
            return stage
    return None


def stage_allowed(stage, launch):
    precision = safe_get(launch, "net_precision", "id")
    status_id = safe_get(launch, "status", "id")
    if status_id not in ALERT_STATUSES:
        return False
    if stage["key"] == "day":
        return precision in HOUR_OR_BETTER
    # 1 hour and final alerts need an exact time and a confirmed Go.
    return precision in PRECISE and status_id == 1


# --------------------------------------------------------------------- MAIN


def main():
    dry_run = "--dry-run" in sys.argv
    test = "--test" in sys.argv or os.environ.get("TEST_ALERT", "").lower() == "true"
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url and not dry_run:
        log("DISCORD_WEBHOOK_URL is not set. Add it as a repository secret.")
        return 1

    now = datetime.now(timezone.utc)
    fixture = os.environ.get("LAUNCH_FIXTURE")  # for local testing only
    try:
        if fixture:
            with open(fixture, encoding="utf-8") as f:
                launches = json.load(f).get("results", [])
            fake_now = os.environ.get("FAKE_NOW")
            if fake_now:
                now = parse_time(fake_now)
        else:
            launches = fetch_launches(now)
    except urllib.error.HTTPError as err:
        if err.code == 429:
            log("Launch Library rate limit hit. Skipping this run, will try again next time.")
            return 0
        log(f"Could not get launches ({err.code}). Skipping this run.")
        return 0
    except Exception as err:  # network hiccup: try again next run
        log(f"Could not get launches: {err}. Skipping this run.")
        return 0

    log(f"{len(launches)} launches in the next 26 hours ({now:%Y-%m-%d %H:%M} UTC)")

    if test:
        upcoming = [l for l in launches if l.get("net") and parse_time(l["net"]) > now]
        if not upcoming:
            log("No upcoming launches to use for the test.")
            return 0
        launch = upcoming[0]
        payload = build_message(launch, STAGES[1], parse_time(launch["net"]), False, test=True)
        if dry_run:
            log(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        ok = post_to_discord(webhook_url, payload)
        log("Test alert sent." if ok else "Test alert FAILED.")
        return 0 if ok else 1

    state = load_state()
    changed = False
    posts = 0

    for launch in launches:
        launch_id = launch.get("id")
        if not launch_id or not launch.get("net"):
            continue
        net = parse_time(launch["net"])
        minutes_left = (net - now).total_seconds() / 60
        record = state.get(launch_id)

        if record:
            old_net = parse_time(record["net"])
            if abs((net - old_net).total_seconds()) / 60 > RESCHEDULE_MINUTES:
                log(f"{launch.get('name')}: time changed, alerts reset")
                was_alerted = bool(record.get("sent")) or record.get("rescheduled", False)
                record = {"net": launch["net"], "sent": [], "name": launch.get("name"),
                          "rescheduled": was_alerted}
                state[launch_id] = record
                changed = True
            elif record["net"] != launch["net"]:
                record["net"] = launch["net"]  # small slip: keep alerts, track new time
                changed = True

        stage = due_stage(minutes_left)
        if not stage or not stage_allowed(stage, launch):
            continue
        sent = record["sent"] if record else []
        if stage["key"] in sent:
            continue
        if posts >= MAX_POSTS_PER_RUN:
            log("Post limit for this run reached, the rest will go out next run.")
            break

        rescheduled = bool(record and record.get("rescheduled"))
        payload = build_message(launch, stage, net, rescheduled)
        log(f"Sending {stage['key']} alert: {launch.get('name')} "
            f"({minutes_left:.0f} min to go, roles: {', '.join(matching_roles(launch))})")
        if dry_run:
            log(json.dumps(payload, indent=2, ensure_ascii=False))
            ok = True
        else:
            ok = post_to_discord(webhook_url, payload)
            time.sleep(1.2)
        if not ok:
            continue

        # Mark this alert and any earlier ones as done, so a late first
        # run never sends a stale "24 hours to go" after the 1 hour alert.
        index = next(i for i, s in enumerate(STAGES) if s["key"] == stage["key"])
        done = {s["key"] for s in STAGES[: index + 1]} | set(sent)
        state[launch_id] = {"net": launch["net"], "name": launch.get("name"),
                            "sent": [s["key"] for s in STAGES if s["key"] in done]}
        changed = True
        posts += 1

    # Forget launches that went more than 2 days ago.
    cutoff = now - timedelta(days=2)
    for launch_id in list(state):
        try:
            if parse_time(state[launch_id]["net"]) < cutoff:
                del state[launch_id]
                changed = True
        except (KeyError, ValueError):
            del state[launch_id]
            changed = True

    if changed and not dry_run:
        save_state(state)
    log(f"Done. {posts} alert(s) sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
