#!/usr/bin/env python3
"""
Vendor restock roller for Discord.

Reads vendors.yaml, works out which vendors are due to restock today,
rolls their stock, and posts it to each vendor's Discord channel or
forum post through a webhook.

Usage:
  python roll_vendors.py                  # roll every vendor due today
  python roll_vendors.py --vendor all     # roll every vendor now
  python roll_vendors.py --vendor jacks_shack --force
  python roll_vendors.py --vendor all --dry-run   # print, don't post
"""
import argparse
import ast
import datetime as dt
import json
import operator
import os
import random
import re
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "vendors.yaml")
STATE_PATH = os.path.join(HERE, "state.json")
USER_AGENT = "DiscordBot (vendor-restock, 1.0)"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MAX_MESSAGE = 1900  # Discord's limit is 2000; leave some room


# ---------------------------------------------------------------- dice

_DICE = re.compile(r"(\d*)\s*[dD]\s*(\d*)")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.FloorDiv: operator.floordiv,
        ast.Div: operator.floordiv}


def roll(expr):
    """Roll a dice expression such as '2D', '1D3+1', '2D6*1000+1D1000' or '5'.
    'D' alone means D6, as in Traveller."""
    if isinstance(expr, (int, float)):
        return int(expr)
    text = str(expr).strip()

    def repl(m):
        n = int(m.group(1) or 1)
        sides = int(m.group(2) or 6)
        return str(sum(random.randint(1, sides) for _ in range(n)))

    text = _DICE.sub(repl, text)
    return int(_safe_eval(ast.parse(text, mode="eval").body))


def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_safe_eval(node.operand)
    raise ValueError("Unsupported dice expression")


# ---------------------------------------------------------------- stock

def weighted_sample(pool, count):
    """Pick `count` different items from the pool, favouring higher weights."""
    items = [dict(i) if isinstance(i, dict) else {"name": str(i)} for i in pool]
    chosen = []
    while items and len(chosen) < count:
        total = sum(float(i.get("weight", 1)) for i in items)
        pick = random.uniform(0, total)
        run = 0.0
        for idx, item in enumerate(items):
            run += float(item.get("weight", 1))
            if pick <= run:
                chosen.append(items.pop(idx))
                break
    return chosen


def roll_section(section):
    lines = []
    always = section.get("always", []) or []
    pool = section.get("pool", []) or []
    count = max(0, roll(section.get("count", 0))) if pool else 0
    picked = [dict(i) if isinstance(i, dict) else {"name": str(i)} for i in always]
    picked += weighted_sample(pool, count)
    show_qty = section.get("show_quantity", True)
    for item in picked:
        name = item["name"]
        if show_qty:
            qty = max(1, roll(item.get("qty", 1)))
            lines.append(f"{name} x{qty}")
        else:
            lines.append(name)
    return lines


def format_budget(vendor):
    budget = vendor.get("budget")
    if budget in (None, ""):
        return None
    label = vendor.get("budget_label", "Monthly Purchasing Budget")
    text = str(budget)
    if re.fullmatch(r"[\d\sdD+\-*/()]+", text) and re.search(r"\d", text):
        text = f"{roll(text):,}Cr"
    return f"*{label}:* {text}"


def build_message(vendor):
    parts = []
    if vendor.get("header"):
        parts.append(f"### {vendor['header'].strip()}")
    if vendor.get("notes"):
        parts.append(vendor["notes"].strip())
    label = f"**{vendor['stock_label']}**" if vendor.get("stock_label") else None
    for section in vendor.get("sections", []):
        lines = roll_section(section)
        if not lines:
            continue
        block = "\n".join(lines)
        if section.get("title"):
            block = f"*{section['title']}:*\n{block}"
        if label:
            block, label = f"{label}\n{block}", None
        parts.append(block)
    budget = format_budget(vendor)
    if budget:
        parts.append(budget)
    if vendor.get("footer"):
        parts.append(f"-# {vendor['footer'].strip()}")
    return parts


def split_message(parts):
    """Join parts with blank lines, splitting into several messages if needed."""
    messages, current = [], ""
    for part in parts:
        candidate = f"{current}\n\n{part}" if current else part
        if len(candidate) > MAX_MESSAGE and current:
            messages.append(current)
            current = part
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


# ---------------------------------------------------------------- schedule

def is_due(vendor, today):
    reset = str(vendor.get("reset", "static")).lower()
    if reset == "weekly":
        day = str(vendor.get("reset_day", "monday")).lower()
        return WEEKDAYS[today.weekday()] == day
    if reset == "monthly":
        day = int(vendor.get("reset_day", 1))
        next_day = today + dt.timedelta(days=1)
        last_of_month = next_day.month != today.month
        return today.day == day or (last_of_month and day > today.day)
    if reset == "daily":
        return True
    return False  # static vendors only post when run by hand


# ---------------------------------------------------------------- discord

def webhook_url(vendor, secrets):
    name = vendor.get("webhook_secret", "")
    return secrets.get(name) or os.environ.get(name, "")


def discord_request(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"Discord returned {e.code}: {detail}") from None


def with_thread(url, vendor, extra=""):
    params = []
    if vendor.get("thread_id"):
        params.append(f"thread_id={vendor['thread_id']}")
    if extra:
        params.append(extra)
    return url + ("?" + "&".join(params) if params else "")


def post_vendor(vendor, messages, url, previous_ids):
    if vendor.get("delete_previous") and previous_ids:
        for mid in previous_ids:
            try:
                discord_request("DELETE", with_thread(f"{url}/messages/{mid}", vendor))
            except RuntimeError as e:
                print(f"  (could not delete old message {mid}: {e})")
    ids = []
    for i, content in enumerate(messages):
        payload = {"content": content, "allowed_mentions": {"parse": []}}
        if vendor.get("username"):
            payload["username"] = vendor["username"]
        if vendor.get("avatar_url"):
            payload["avatar_url"] = vendor["avatar_url"]
        if vendor.get("image_url") and i == len(messages) - 1:
            payload["embeds"] = [{"image": {"url": vendor["image_url"]}}]
        result = discord_request("POST", with_thread(url, vendor, "wait=true"), payload)
        ids.append(result.get("id"))
    return [i for i in ids if i]


# ---------------------------------------------------------------- main

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", default="", help="vendor id, or 'all'. Blank = vendors due today")
    ap.add_argument("--force", action="store_true", help="roll even if already rolled today")
    ap.add_argument("--dry-run", action="store_true", help="print the stock instead of posting")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    tz = ZoneInfo(config.get("timezone", "UTC"))
    today = dt.datetime.now(tz).date()
    state = load_json(STATE_PATH, {})
    secrets = json.loads(os.environ.get("SECRETS_JSON", "{}") or "{}")

    vendors = config.get("vendors", {})
    if args.vendor and args.vendor != "all":
        if args.vendor not in vendors:
            sys.exit(f"Unknown vendor '{args.vendor}'. Options: {', '.join(vendors)}")
        selected = {args.vendor: vendors[args.vendor]}
        manual = True
    else:
        selected = vendors
        manual = args.vendor == "all"

    failures = 0
    for vid, vendor in selected.items():
        if not vendor.get("enabled", True):
            continue
        if not manual and not is_due(vendor, today):
            continue
        last = state.get(vid, {}).get("last_rolled")
        if last == today.isoformat() and not (args.force or args.dry_run):
            print(f"{vid}: already rolled today, skipping (use --force to reroll)")
            continue

        messages = split_message(build_message(vendor))
        print(f"\n=== {vendor.get('name', vid)} ===")
        for m in messages:
            print(m, "\n")
        if args.dry_run:
            continue

        url = webhook_url(vendor, secrets)
        if not url:
            print(f"{vid}: no webhook found in secret '{vendor.get('webhook_secret')}', skipping")
            failures += 1
            continue
        try:
            ids = post_vendor(vendor, messages, url, state.get(vid, {}).get("message_ids", []))
            state[vid] = {"last_rolled": today.isoformat(), "message_ids": ids}
            print(f"{vid}: posted")
        except RuntimeError as e:
            print(f"{vid}: FAILED - {e}")
            failures += 1

    if not args.dry_run:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
