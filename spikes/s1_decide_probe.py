"""Throwaway S1 probe of the Mercury Decide API via OpenRouter.

Usage: uv run --with httpx spikes/s1_decide_probe.py <mode> [extra]
Modes: basic | image | latency | burst | paid | models | variants | errors
The API key is read from OPENROUTER_API_KEY and is never printed.
"""
import asyncio
import base64
import json
import os
import statistics
import struct
import sys
import time
import zlib

import httpx

BASE = "https://openrouter.ai/api/v1"
URL = f"{BASE}/systemone"
FREE = "inception/mercury-decide:free"
PAID = "inception/mercury-decide"

STATE = {"ticket": "I was charged twice for my subscription this month and the app keeps crashing."}
BOOL = "noul"  # OpenRouter rejects "boolean"; valid: noul | choice | score | box
QUESTIONS = {
    "is_billing_issue": {
        "type": BOOL,
        "instructions": "Does the ticket concern a billing problem?",
    },
    "category": {
        "type": "choice",
        "instructions": "Which category best fits the ticket?",
        "criteria": {
            "billing": "Charges, refunds, invoices",
            "bug": "Software crashes or malfunctions",
            "other": "Anything else",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is the ticket?",
        "criteria": ["not urgent", "somewhat urgent", "urgent", "critical"],
    },
}


def key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k:
        sys.exit("OPENROUTER_API_KEY not set")
    return k


def headers() -> dict:
    return {"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}


def body(model=FREE, state=STATE, questions=QUESTIONS, **extra) -> dict:
    return {"model": model, "state": state, "questions": questions, **extra}


def show_headers(r: httpx.Response) -> dict:
    return {k: v for k, v in r.headers.items() if k.lower() not in ("set-cookie", "authorization")}


def post(payload, url=URL, client=None):
    t0 = time.perf_counter()
    c = client or httpx.Client(timeout=60)
    r = c.post(url, headers=headers(), json=payload)
    dt = time.perf_counter() - t0
    return r, dt


def dump(label, payload, r, dt, hdrs=False):
    print(f"=== {label}")
    print("REQUEST:", json.dumps(payload, indent=2)[:3000])
    print(f"STATUS: {r.status_code}  ({dt*1000:.0f} ms)")
    if hdrs:
        print("HEADERS:", json.dumps(show_headers(r), indent=2))
    try:
        print("BODY:", json.dumps(r.json(), indent=2))
    except Exception:
        print("BODY(raw):", r.text[:2000])


def make_png() -> bytes:
    """200x100 PNG with 'ERROR 500' drawn from a tiny 5x7 bitmap font (no deps)."""
    font = {
        "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
        "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
        "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
        "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
        "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
        " ": ["00000"] * 7,
    }
    w, h, scale = 200, 100, 3
    text = "ERROR 500"
    px = [[255] * w for _ in range(h)]
    tw = len(text) * 6 * scale
    x0, y0 = (w - tw) // 2, (h - 7 * scale) // 2
    for i, ch in enumerate(text):
        for ry, row in enumerate(font[ch]):
            for rx, bit in enumerate(row):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            px[y0 + ry * scale + dy][x0 + (i * 6 + rx) * scale + dx] = 0
    raw = b"".join(b"\x00" + bytes(row) for row in px)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mode_basic(model=FREE):
    p = body(model)
    r, dt = post(p)
    dump(f"basic {model}", p, r, dt, hdrs=True)
    if r.status_code == 404:
        for alt in ("decisions", "evaluate"):
            r2, dt2 = post(p, url=f"{BASE}/{alt}")
            dump(f"basic alt endpoint /{alt}", p, r2, dt2)


def mode_image():
    b64 = base64.b64encode(make_png()).decode()
    state = [{"role": "user", "content": [
        {"type": "text", "text": "Screenshot of the failing page."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]}]
    qs = {"shows_error": {"type": BOOL, "instructions": "Does the image show an error message?"}}
    p = body(FREE, state=state, questions=qs)
    r, dt = post(p)
    shown = json.loads(json.dumps(p))
    shown["state"][0]["content"][1]["image_url"]["url"] = f"data:image/png;base64,<{len(b64)} chars>"
    dump("image", shown, r, dt, hdrs=True)


def blank_png() -> bytes:
    w, h = 200, 100
    raw = b"".join(b"\x00" + bytes([255] * w) for _ in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mode_image2():
    """Image-only control: does the model actually read pixels? (2 calls)"""
    qs = {"has_500": {"type": BOOL, "instructions": "Does the image contain the text 'ERROR 500'?"}}
    for label, png in (("error500", make_png()), ("blank", blank_png())):
        b64 = base64.b64encode(png).decode()
        state = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
        p = body(FREE, state=state, questions=qs)
        r, dt = post(p)
        shown = json.loads(json.dumps(p))
        shown["state"][0]["content"][0]["image_url"]["url"] = f"data:image/png;base64,<{len(b64)} chars>"
        dump(f"image-only {label}", shown, r, dt)


def pct(xs, q):
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def mode_latency(model=FREE, n=10):
    times, statuses = [], []
    with httpx.Client(timeout=60) as c:
        for i in range(n):
            r, dt = post(body(model), client=c)
            times.append(dt * 1000)
            statuses.append(r.status_code)
            print(f"call {i}: {r.status_code} {dt*1000:.0f} ms")
            if r.status_code != 200:
                print("  body:", r.text[:300])
    ok = [t for t, s in zip(times, statuses) if s == 200]
    if ok:
        print(f"ok={len(ok)}/{n} p50={statistics.median(ok):.0f} ms p95={pct(ok,0.95):.0f} ms min={min(ok):.0f} max={max(ok):.0f}")


async def mode_burst(n=40):
    results = []
    async with httpx.AsyncClient(timeout=120) as c:
        async def one(i):
            t0 = time.perf_counter()
            r = await c.post(URL, headers=headers(), json=body(FREE))
            results.append((i, r.status_code, (time.perf_counter() - t0) * 1000, show_headers(r), r.text[:400]))
        await asyncio.gather(*(one(i) for i in range(n)))
    from collections import Counter
    print("status counts:", dict(Counter(s for _, s, *_ in results)))
    first_bad = next((x for x in results if x[1] != 200), None)
    if first_bad:
        print("first non-200 headers:", json.dumps(first_bad[3], indent=2))
        print("first non-200 body:", first_bad[4])
    ok = [t for _, s, t, *_ in results if s == 200]
    if ok:
        print(f"200 latency p50={statistics.median(ok):.0f} ms p95={pct(ok,0.95):.0f} ms")
    print("distinct non-200 bodies:", {x[4] for x in results if x[1] != 200})
    print("Retry-After values:", {x[3].get("retry-after") for x in results if x[1] != 200})


def mode_paid():
    p = body(PAID)
    r, dt = post(p)
    dump("paid", p, r, dt, hdrs=True)


def mode_models():
    # decision models are only listed when output_modalities includes "decisions" (or "all")
    r = httpx.get(f"{BASE}/models", params={"output_modalities": "all"}, timeout=60)
    for m in r.json().get("data", []):
        if "mercury-decide" in m["id"]:
            print(json.dumps(m, indent=2))


def mode_variants():
    # Each variant is one free call.
    v = []
    bool_q = {"q": {"type": BOOL, "instructions": "Is this about billing?"}}
    v.append(("input instead of state", {"model": FREE, "input": STATE, "questions": bool_q}))
    v.append(("predicate type", body(state=STATE, questions={"q": {"type": "predicate", "instructions": "Is this about billing?"}})))
    v.append(("noul type", body(state=STATE, questions={"q": {"type": "noul", "instructions": "Is this about billing?"}})))
    v.append(("boolean w/ criteria true/false", body(state=STATE, questions={"q": {"type": BOOL, "instructions": "Is this about billing?", "criteria": {"true": "billing", "false": "not billing"}}})))
    v.append(("state as plain string", body(state="I was charged twice.", questions=bool_q)))
    for label, p in v:
        r, dt = post(p)
        dump(label, p, r, dt)


def mode_errors():
    # Both are rejected before inference (no model quota used).
    p = body(model="inception/does-not-exist")
    r, dt = post(p)
    dump("unknown model", p, r, dt)
    r = httpx.post(URL, headers={"Authorization": "Bearer invalid", "Content-Type": "application/json"}, json=body(), timeout=60)
    dump("invalid api key", body(), r, 0, hdrs=True)


if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "basic"
    if m == "burst":
        asyncio.run(mode_burst())
    else:
        {"basic": mode_basic, "image": mode_image, "image2": mode_image2, "latency": mode_latency, "paid": mode_paid,
         "models": mode_models, "variants": mode_variants, "errors": mode_errors}[m]()
