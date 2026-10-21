"""
iPhone Duo availability watcher.

Opens Apple's store page for one configuration in a headless browser, asks
Apple's own fulfillment service for delivery and pickup info at each ZIP code,
compares it with the last run, and sends a phone push (ntfy.sh) when anything
changes.

Settings come from environment variables (see README.md). Defaults are set for
iPhone Duo 1TB Night Sky Unlocked, ZIPs 02130 and 02115.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-1tb-night-sky-unlocked",
)
ZIPS = [z.strip() for z in os.environ.get("ZIPS", "02130,02115").split(",") if z.strip()]
PART_NUMBER = os.environ.get("PART_NUMBER", "").strip()  # optional override, e.g. MXXXXLL/A
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MAX_STORES = int(os.environ.get("MAX_STORES", "6"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Words that identify this configuration inside the page's product data.
MATCH_WORDS = [w.strip().lower() for w in os.environ.get(
    "MATCH_WORDS", "1tb,night,unlocked").split(",") if w.strip()]

KEYWORDS = re.compile(
    r"(deliver|ship|pickup|pick up|available|unavailable|check back|in stock|"
    r"arrives|backorder|\b\d{1,2}\.\d{1,2}\b|jan|feb|mar|oct|nov|dec)",
    re.I,
)


# ---------------------------------------------------------------- notify

def notify(title: str, message: str, priority: str = "high") -> None:
    print(f"\n=== NOTIFY: {title}\n{message}\n")
    if not NTFY_TOPIC:
        print("(NTFY_TOPIC not set, skipping push)")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": "iphone",
            "Click": PRODUCT_URL,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # never let a notify failure kill the run
        print(f"ntfy push failed: {e}")


# ---------------------------------------------------------------- parsing

def find_part_number(html: str) -> str | None:
    """Find the part number for this config in the page's embedded product data."""
    candidates = {}
    for m in re.finditer(r'"partNumber"\s*:\s*"([A-Z0-9]{4,6}[A-Z]{1,2}/A)"', html):
        window = html[max(0, m.start() - 800): m.end() + 800].lower()
        window = window.replace("-", "").replace("_", "").replace(" ", "")
        score = sum(1 for w in MATCH_WORDS if w.replace(" ", "") in window)
        candidates[m.group(1)] = max(score, candidates.get(m.group(1), 0))
    if not candidates:
        return None
    best = max(candidates.items(), key=lambda kv: kv[1])
    return best[0] if best[1] == len(MATCH_WORDS) else None


def summarize_fulfillment(data: dict, part: str) -> dict:
    """Pull the human-readable delivery and pickup lines out of Apple's JSON."""
    content = (data or {}).get("body", {}).get("content", {}) or {}
    out = {"delivery": [], "pickup": []}

    # Delivery
    dm = content.get("deliveryMessage", {}) or {}
    pdm = dm.get(part) or next((v for k, v in dm.items() if isinstance(v, dict)), {}) or {}
    for key in ("regular", "express"):
        block = pdm.get(key) or {}
        for opt in block.get("deliveryOptionMessages", []) or []:
            txt = opt.get("displayName") if isinstance(opt, dict) else str(opt)
            if txt:
                out["delivery"].append(txt.strip())
        for opt in block.get("deliveryOptions", []) or []:
            if isinstance(opt, dict):
                bits = [opt.get("displayName"), opt.get("date"), opt.get("shippingCost")]
                txt = " | ".join(str(b) for b in bits if b)
                if txt:
                    out["delivery"].append(txt.strip())
    out["delivery"] = sorted(set(out["delivery"]))

    # Pickup
    stores = (content.get("pickupMessage", {}) or {}).get("stores", []) or []
    for s in stores[:MAX_STORES]:
        pa = (s.get("partsAvailability") or {})
        info = pa.get(part) or next(iter(pa.values()), {}) if pa else {}
        name = s.get("storeName") or s.get("storeNumber") or "store"
        quote = (info.get("pickupSearchQuote") or info.get("messageTypes", {})
                 .get("regular", {}).get("storePickupQuote") or "")
        status = info.get("pickupDisplay") or ""
        out["pickup"].append(f"{name}: {status} {quote}".strip())
    # generic pickup notice (e.g. "Check back for availability on 10.24")
    pm = content.get("pickupMessage", {}) or {}
    for k in ("notAvailableNearby", "pickupLocation", "noSimilarModelsText"):
        if isinstance(pm.get(k), str) and pm[k].strip():
            out["pickup"].append(pm[k].strip())
    return out


def page_text_lines(page) -> list[str]:
    """Fallback: visible lines on the page that talk about dates/availability."""
    try:
        text = page.inner_text("body", timeout=10000)
    except Exception:
        return []
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if 4 <= len(line) <= 160 and KEYWORDS.search(line):
            lines.append(line)
    # drop the long legal footnotes
    return sorted(set(l for l in lines if "footnote" not in l.lower()))[:40]


# ---------------------------------------------------------------- main check

def run_check() -> dict:
    result = {"part": None, "zips": {}, "page_lines": [], "errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-US",
            timezone_id="America/New_York",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/129.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()

        # Remember any fulfillment calls the page makes itself (gives us the part #)
        seen_parts = []
        def on_request(req):
            if "fulfillment-messages" in req.url or "delivery-message" in req.url:
                seen_parts.extend(re.findall(r"parts\.\d=([A-Z0-9%]+?(?:%2F|/)A)", req.url))
        page.on("request", on_request)

        page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        html = page.content()

        part = PART_NUMBER or find_part_number(html)
        if not part and seen_parts:
            part = seen_parts[0].replace("%2F", "/")
        result["part"] = part
        result["page_lines"] = page_text_lines(page)

        if not part:
            result["errors"].append(
                "Couldn't find the part number on the page. Set PART_NUMBER "
                "(see README) - falling back to watching page text only.")
        else:
            for z in ZIPS:
                url = ("/shop/fulfillment-messages?pl=true&mts.0=regular&mts.1=compact"
                       f"&cppart=UNLOCKED/US&parts.0={part}&location={z}")
                try:
                    data = page.evaluate(
                        """async (u) => {
                            const r = await fetch(u, {credentials: 'include',
                                headers: {'Accept': 'application/json'}});
                            if (!r.ok) return {__status: r.status};
                            return await r.json();
                        }""", url)
                    if isinstance(data, dict) and "__status" in data:
                        result["errors"].append(f"ZIP {z}: Apple returned HTTP {data['__status']}")
                        continue
                    result["zips"][z] = summarize_fulfillment(data, part)
                except Exception as e:
                    result["errors"].append(f"ZIP {z}: {e}")
        browser.close()
    return result


def fingerprint(res: dict) -> dict:
    """The parts we compare between runs."""
    return {"zips": res["zips"], "page_lines": res["page_lines"]}


def describe(res: dict) -> str:
    parts = []
    for z, info in res["zips"].items():
        parts.append(f"ZIP {z}")
        parts.append("  Delivery: " + ("; ".join(info["delivery"]) or "no date shown"))
        if info["pickup"]:
            parts.append("  Pickup:")
            parts += [f"   - {s}" for s in info["pickup"]]
    if not res["zips"] and res["page_lines"]:
        parts.append("Page says:")
        parts += [f" - {l}" for l in res["page_lines"][:12]]
    return "\n".join(parts) or "Nothing readable found."


def diff_text(old: dict, new: dict) -> str:
    lines = []
    for z in sorted(set(old.get("zips", {})) | set(new.get("zips", {}))):
        o, n = old.get("zips", {}).get(z, {}), new.get("zips", {}).get(z, {})
        for key in ("delivery", "pickup"):
            before, after = set(o.get(key, [])), set(n.get(key, []))
            for x in sorted(after - before):
                lines.append(f"[{z} {key}] NOW: {x}")
            for x in sorted(before - after):
                lines.append(f"[{z} {key}] was: {x}")
    if not new.get("zips"):
        b, a = set(old.get("page_lines", [])), set(new.get("page_lines", []))
        lines += [f"NOW: {x}" for x in sorted(a - b)][:10]
        lines += [f"was: {x}" for x in sorted(b - a)][:5]
    return "\n".join(lines)


def main() -> int:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        res = run_check()
    except Exception as e:
        res = {"part": None, "zips": {}, "page_lines": [], "errors": [f"Run failed: {e}"]}

    print(json.dumps(res, indent=2))
    fp = fingerprint(res)
    got_data = bool(res["zips"]) or bool(res["page_lines"])

    if not got_data:
        fails = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fails
        if fails == 3:  # tell you once if it keeps failing, not every hour
            notify("iPhone Duo watcher is failing",
                   "3 checks in a row couldn't read Apple's page.\n" + "\n".join(res["errors"]),
                   priority="default")
    else:
        state["consecutive_failures"] = 0
        old = state.get("last")
        if old is None:
            notify("iPhone Duo watcher started", describe(res), priority="default")
        elif old != fp:
            notify("iPhone Duo update!", diff_text(old, fp) + "\n\nCurrent:\n" + describe(res))
        else:
            print("No change.")
        state["last"] = fp
        state["part"] = res["part"]
        state["last_change_check"] = now

    state["last_run"] = now
    state["last_errors"] = res["errors"]
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
