#!/usr/bin/env python3
"""
PON Fleet — one control server for many PON Lab Raspberry Pis.

Each deployed Pi runs PON Lab (relays -> meters). PON Fleet talks to them by IP,
lets you pick up to 16 meters across any number of Pis, and cuts / restores /
runs sequences on all of them at the same instant. It corrects for each Pi's
clock offset, then reports how closely the cuts actually lined up (skew), and
gathers every Pi's marks into one results table and CSV.

Only needs Flask. Runs on a Pi, a laptop or a Windows PC.
"""
import csv
import io
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_from_directory

VERSION = "1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("PONFLEET_DATA") or os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
CFG_PATH = os.path.join(BASE, "fleet-config.json")
NODES_PATH = os.path.join(DATA, "nodes.json")
GROUPS_PATH = os.path.join(DATA, "groups.json")
RUNS_PATH = os.path.join(DATA, "runs.jsonl")

with open(CFG_PATH) as f:
    CFG = json.load(f)
MAX_TARGETS = CFG.get("max_targets", 16)
POLL_S = CFG.get("poll_seconds", 1.0)
pool = ThreadPoolExecutor(max_workers=32)
lock = threading.RLock()


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


nodes = {n["id"]: n for n in load(NODES_PATH, [])}
groups = load(GROUPS_PATH, [])
runs = []
if os.path.exists(RUNS_PATH):
    for line in open(RUNS_PATH):
        try:
            runs.append(json.loads(line))
        except Exception:
            pass

# live info per node (not saved)
live = {}


def blank_live():
    return {"online": False, "error": "not contacted yet", "last_ok": None, "rtt_ms": None,
            "offset": 0.0, "offset_ok": False, "state": None, "events": {}, "since": None,
            "last_time_sync": 0}


def save_nodes():
    save(NODES_PATH, list(nodes.values()))


# ----------------------------------------------------------- talking to Pis
def url_of(node, path):
    host = node["host"].strip()
    if not host.startswith("http"):
        host = "http://" + host
    if host.count(":") < 2:  # no port given
        host += ":8080"
    return host.rstrip("/") + path


def call(node, path, body=None, timeout=3.0):
    data = None
    headers = {"Content-Type": "application/json"}
    if node.get("token"):
        headers["X-PonLab-Token"] = node["token"]
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url_of(node, path), data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def sync_clock(node):
    """Estimate this Pi's clock offset with a few quick pings (keeps the best RTT)."""
    best = None
    for _ in range(5):
        t0 = time.time()
        r = call(node, "/api/time", timeout=2)
        t1 = time.time()
        rtt = t1 - t0
        off = r["t"] - (t0 + t1) / 2
        if best is None or rtt < best[0]:
            best = (rtt, off)
    return best


def poll_node(nid):
    while nid in nodes:
        node = nodes.get(nid)
        L = live.setdefault(nid, blank_live())
        try:
            if time.time() - L["last_time_sync"] > CFG.get("clock_sync_seconds", 30) or not L["offset_ok"]:
                rtt, off = sync_clock(node)
                L.update(offset=off, offset_ok=True, rtt_ms=round(rtt * 1000, 1), last_time_sync=time.time())
            q = f"/api/state?since={L['since']}" if L["since"] else "/api/state"
            st = call(node, q)
            with lock:
                for e in st.get("events", []):
                    L["events"][e["id"]] = e
                if len(L["events"]) > 3000:
                    keep = sorted(L["events"].values(), key=lambda e: e["t"])[-2500:]
                    L["events"] = {e["id"]: e for e in keep}
                if L["events"]:
                    L["since"] = max(e["t"] for e in L["events"].values()) - 0.001
                st.pop("events", None)
                L.update(online=True, error=None, last_ok=time.time(), state=st)
        except urllib.error.HTTPError as e:
            L.update(online=False, error=f"HTTP {e.code}" + (" (wrong token?)" if e.code == 401 else ""))
        except Exception as e:
            msg = str(getattr(e, "reason", e))
            if "refused" in msg:
                msg = "Pi answers but PON Lab isn't running"
            elif "timed out" in msg or "unreachable" in msg.lower() or "No route" in msg:
                msg = "can't reach this Pi (off, unplugged or wrong IP?)"
            L.update(online=False, error=msg[:80], offset_ok=False)
        time.sleep(POLL_S if L["online"] else 3)


def start_poller(nid):
    live[nid] = blank_live()
    threading.Thread(target=poll_node, args=(nid,), daemon=True).start()


for nid in list(nodes):
    start_poller(nid)


# --------------------------------------------------------------- fleet ops
def parse_targets(targets):
    by_node = {}
    for t in targets:
        nid, ch = t.split(":", 1)
        if nid in nodes:
            by_node.setdefault(nid, []).append(ch)
    return by_node


def fleet_run(targets, op, lead, **params):
    """Fire one action on many meters at the same instant."""
    if op != "on" and len(targets) > MAX_TARGETS:
        raise ValueError(f"pick at most {MAX_TARGETS} meters")
    run_id = "R" + uuid.uuid4().hex[:6]
    at = time.time() + lead
    by_node = parse_targets(targets)

    def send(nid):
        node, L = nodes[nid], live.get(nid, {})
        acts = [{"ch": ch, "op": op, **params} for ch in by_node[nid]]
        at_node = at + (L.get("offset") or 0)  # convert to that Pi's clock
        try:
            r = call(node, "/api/batch", {"at": at_node, "run": run_id, "actions": acts}, timeout=lead + 2)
            return nid, {"ok": True, "count": r.get("count")}
        except urllib.error.HTTPError as e:
            return nid, {"ok": False, "error": "wrong or missing access token" if e.code == 401 else f"HTTP {e.code}"}
        except Exception as e:
            return nid, {"ok": False, "error": str(getattr(e, "reason", e))[:80]}

    results = dict(pool.map(send, list(by_node)))
    run = {"id": run_id, "t": time.time(), "at": at, "op": op, "targets": targets,
           "name": params.get("name") or params.get("reason") or op, "params": params,
           "send": results}
    with lock:
        runs.append(run)
        with open(RUNS_PATH, "a") as f:
            f.write(json.dumps(run) + "\n")
    return run


def fleet_events():
    """All Pis' events with times converted to the fleet server's clock."""
    out = []
    for nid, L in live.items():
        off = L.get("offset") or 0
        for e in L["events"].values():
            out.append({**e, "node": nid, "ft": e["t"] - off})
    out.sort(key=lambda e: e["ft"])
    return out


def run_results(run, evs):
    """Per-meter results for one fleet run: actual cut time, skew, marks."""
    mtypes = [m["key"] for m in CFG.get("mark_types", [])]
    rows, cut_times = [], []
    for tgt in run["targets"]:
        nid, ch = tgt.split(":", 1)
        mine = [e for e in evs if e["node"] == nid and e["channel"] == ch]
        offs = [e for e in mine if e["kind"] == "power_off" and e.get("run") == run["id"]]
        row = {"target": tgt, "node": nodes.get(nid, {}).get("name", nid), "ch": ch,
               "meter": meter_name(nid, ch), "outages": len(offs)}
        if offs:
            first = offs[0]
            cut_times.append(first["ft"])
            row["cut_at"] = first["ft"]
            ons = [e for e in mine if e["kind"] == "power_on" and e["ft"] > first["ft"]]
            end = ons[0]["ft"] if ons else None
            row["outage_s"] = ons[0].get("outage_seconds") if ons else None
            window_end = (end or time.time()) + 1800
            later = [e["ft"] for e in mine if e["kind"] == "power_off" and e["ft"] > first["ft"]
                     and e.get("run") != run["id"]]
            if later:  # marks after the next, unrelated outage belong to that one
                window_end = min(window_end, later[0])
            for k in mtypes:
                m = [e for e in mine if e["kind"] == "mark:" + k and first["ft"] <= e["ft"] <= window_end]
                if m:
                    row[k] = round(m[0]["ft"] - first["ft"], 1)
            sd = [e for e in mine if e["kind"] == "sense_dark" and e["ft"] >= first["ft"]]
            if sd:
                row["sense_dark"] = sd[0].get("seconds_into_outage")
        rows.append(row)
    skew = round((max(cut_times) - min(cut_times)) * 1000, 1) if len(cut_times) > 1 else None
    return {"rows": rows, "skew_ms": skew, "landed": len(cut_times)}


def meter_name(nid, ch):
    st = (live.get(nid) or {}).get("state") or {}
    for c in st.get("channels", []):
        if c["id"] == ch:
            return c.get("meter") or c.get("name") or ch
    return ch


# ------------------------------------------------------------------- web
app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/pf.js")
def probe_js():
    port = request.host.split(":")[-1] if ":" in request.host else "80"
    return Response(f"window.__ponfleet=(window.__ponfleet||[]).concat([{port}]);",
                    mimetype="application/javascript", headers={"Cache-Control": "no-store"})


@app.get("/api/fleet")
def api_fleet():
    now = time.time()
    evs = fleet_events()
    out = []
    for nid, n in nodes.items():
        L = live.get(nid) or blank_live()
        st = L["state"] or {}
        out.append({"id": nid, "name": n["name"], "host": n["host"], "has_token": bool(n.get("token")),
                    "online": L["online"], "error": L["error"], "rtt_ms": L["rtt_ms"],
                    "offset_ms": round(L["offset"] * 1000, 1) if L["offset_ok"] else None,
                    "stale_s": round(now - L["last_ok"], 1) if L["last_ok"] else None,
                    "sim": st.get("sim"), "node_name": st.get("node"), "token_required": st.get("token_required"), "version": st.get("version"),
                    "dashboard": url_of(n, "/"),
                    "channels": st.get("channels", [])})
    recent = []
    for r in runs[-40:][::-1]:
        recent.append({**{k: r[k] for k in ("id", "t", "at", "op", "name", "targets", "send")},
                       **run_results(r, evs)})
    tail = [{k: e.get(k) for k in ("ft", "node", "channel", "kind", "note", "seconds_into_outage",
                                    "outage_seconds", "run")} for e in evs[-250:][::-1]]
    for e in tail:
        e["node_name"] = nodes.get(e["node"], {}).get("name", e["node"])
    return jsonify({"version": VERSION, "now": now, "max_targets": MAX_TARGETS,
                    "presets": CFG.get("presets", []), "mark_types": CFG.get("mark_types", []),
                    "nodes": out, "runs": recent, "events": tail, "groups": groups})


@app.post("/api/nodes")
def add_node():
    d = request.get_json(silent=True) or {}
    host = (d.get("host") or "").strip()
    if not host:
        return jsonify(ok=False, error="enter an IP address"), 400
    if any(n["host"] == host for n in nodes.values()):
        return jsonify(ok=False, error="already added"), 409
    nid = "n" + uuid.uuid4().hex[:5]
    nodes[nid] = {"id": nid, "host": host, "name": (d.get("name") or host).strip(),
                  "token": (d.get("token") or "").strip()}
    save_nodes()
    start_poller(nid)
    return jsonify(ok=True, id=nid)


@app.post("/api/nodes/<nid>")
def edit_node(nid):
    d = request.get_json(silent=True) or {}
    if nid not in nodes:
        return jsonify(ok=False), 404
    for k in ("name", "host", "token"):
        if k in d:
            nodes[nid][k] = d[k].strip()
    save_nodes()
    live[nid]["offset_ok"] = False
    live[nid]["since"] = None
    return jsonify(ok=True)


@app.delete("/api/nodes/<nid>")
def del_node(nid):
    nodes.pop(nid, None)
    live.pop(nid, None)
    save_nodes()
    return jsonify(ok=True)


@app.post("/api/scan")
def scan():
    """Look for PON Lab Pis on a subnet, e.g. 192.168.1.0/24."""
    d = request.get_json(silent=True) or {}
    try:
        net = ipaddress.ip_network(d.get("subnet", "192.168.1.0/24"), strict=False)
    except ValueError:
        return jsonify(ok=False, error="subnet looks wrong, try 192.168.1.0/24"), 400
    if net.num_addresses > 1024:
        return jsonify(ok=False, error="subnet too big (max /22)"), 400
    port = int(d.get("port", 8080))
    known = {n["host"] for n in nodes.values()}

    def probe(ip):
        host = f"{ip}:{port}" if port != 8080 else str(ip)
        try:
            r = call({"host": f"{ip}:{port}"}, "/api/time", timeout=0.8)
            return {"host": host, "node": r.get("node"), "version": r.get("version"), "known": host in known}
        except Exception:
            return None

    found = [x for x in ThreadPoolExecutor(max_workers=128).map(probe, net.hosts()) if x]
    return jsonify(ok=True, found=found)


@app.post("/api/run")
def api_run():
    d = request.get_json(silent=True) or {}
    targets = d.get("targets") or []
    op = d.get("op")
    if not targets or op not in ("off", "on", "sequence"):
        return jsonify(ok=False, error="pick meters and an action"), 400
    lead = float(d.get("lead", CFG.get("default_lead_seconds", 2)))
    lead = max(0.5, min(lead, 60))
    params = {}
    if op == "off":
        params = {"seconds": d.get("seconds"), "reason": d.get("reason") or (
            f"fleet cut {d['seconds']}s" if d.get("seconds") else "fleet cut")}
    elif op == "on":
        params = {"reason": "fleet restore"}
        lead = 0.5
    elif op == "sequence":
        params = {"steps": d["steps"], "name": d.get("name", "fleet sequence")}
    try:
        return jsonify(ok=True, run=fleet_run(targets, op, lead, **params))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/api/mark")
def api_mark():
    d = request.get_json(silent=True) or {}
    nid, ch = d["target"].split(":", 1)
    try:
        call(nodes[nid], f"/api/{ch}/mark", {"kind": d.get("kind"), "note": d.get("note", "")})
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:80]), 502


@app.post("/api/restore_all")
def restore_all():
    def go(nid):
        try:
            call(nodes[nid], "/api/all/restore", {})
            return True
        except Exception:
            return False
    res = list(pool.map(go, list(nodes)))
    run = {"id": "R" + uuid.uuid4().hex[:6], "t": time.time(), "at": time.time(), "op": "on",
           "name": "RESTORE ALL", "targets": [], "params": {}, "send": {}}
    with lock:
        runs.append(run)
        with open(RUNS_PATH, "a") as f:
            f.write(json.dumps(run) + "\n")
    return jsonify(ok=True, reached=sum(res), total=len(res))


@app.post("/api/groups")
def save_group():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="name the group"), 400
    tg = d.get("targets", [])[:MAX_TARGETS]
    groups[:] = [g for g in groups if g["name"] != name] + [{"name": name, "targets": tg}]
    save(GROUPS_PATH, groups)
    return jsonify(ok=True)


@app.delete("/api/groups/<name>")
def del_group(name):
    groups[:] = [g for g in groups if g["name"] != name]
    save(GROUPS_PATH, groups)
    return jsonify(ok=True)


@app.get("/api/results.csv")
def results_csv():
    evs = fleet_events()
    mt = CFG.get("mark_types", [])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run", "run_name", "run_time", "pi", "channel", "meter", "cut_time", "cut_offset_ms",
                "outage_s"] + [m["label"] + " (s)" for m in mt] + ["sense dark (s)", "run_skew_ms"])
    for r in runs:
        if r["op"] == "on":
            continue
        res = run_results(r, evs)
        first = min((x["cut_at"] for x in res["rows"] if x.get("cut_at")), default=None)
        for x in res["rows"]:
            w.writerow([r["id"], r["name"], datetime.fromtimestamp(r["at"]).isoformat(timespec="seconds"),
                        x["node"], x["ch"], x["meter"],
                        datetime.fromtimestamp(x["cut_at"]).isoformat(timespec="milliseconds") if x.get("cut_at") else "",
                        round((x["cut_at"] - first) * 1000, 1) if x.get("cut_at") and first else "",
                        x.get("outage_s", "")] + [x.get(m["key"], "") for m in mt] +
                       [x.get("sense_dark", ""), res["skew_ms"] if res["skew_ms"] is not None else ""])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ponfleet_results.csv"})


@app.get("/api/events.csv")
def events_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "pi", "channel", "meter", "event", "note", "seconds_into_outage", "outage_s", "run"])
    for e in fleet_events():
        w.writerow([datetime.fromtimestamp(e["ft"]).isoformat(timespec="milliseconds"),
                    nodes.get(e["node"], {}).get("name", e["node"]), e["channel"],
                    meter_name(e["node"], e["channel"]), e["kind"], e.get("note", ""),
                    e.get("seconds_into_outage", ""), e.get("outage_seconds", ""), e.get("run", "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ponfleet_events.csv"})


if __name__ == "__main__":
    import sys
    port = int(os.environ.get("PONFLEET_PORT", CFG.get("port", 8090)))
    print(f"PON Fleet {VERSION} on port {port} — {len(nodes)} Pi(s) configured")
    if "--open" in sys.argv:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    app.run(host="0.0.0.0", port=port, threaded=True)
