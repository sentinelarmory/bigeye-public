"""sentinel-bigeye MCP server: lets agents look at the home cameras through Sentinel-BigEye.

Talks only to the BigEye page on this machine (default http://127.0.0.1:8770, env BIGEYE_URL);
it never touches the cameras or the camera machine directly. Read tools are safe; the few tools
that change something (Home/Away, recording, moving the 360) say so in their description.

House style: FastMCP, @mcp.tool(), registered in ~/.mcp.json.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP, Image

BIGEYE = os.environ.get("BIGEYE_URL", "http://127.0.0.1:8770").rstrip("/")
mcp = FastMCP("sentinel-bigeye")


def _get(path: str, timeout: float = 20, method: str = "GET", raw: bool = False):
    req = urllib.request.Request(BIGEYE + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as reply:
        body = reply.read()
    return body if raw else json.loads(body or b"null")


def _err(e: Exception) -> dict:
    return {"error": f"{type(e).__name__}: {e}", "hint": "Is the BigEye page running? systemctl --user status bigeye-view"}


def _cameras() -> list[str]:
    return list(_get("/state").keys())


@mcp.tool()
def bigeye_status() -> dict:
    """Overview: every camera (live or not, seconds since the last picture, recording, motion), Home/Away,
    where the AI runs (GPU/CPU), the guard's problems and the 360 camera's position. Read-only."""
    try:
        state = _get("/state")
        usage = _get("/usage")
        home = _get("/home")
        cams = {}
        for name, s in state.items():
            cams[name] = {"live": 0 <= s.get("age", -1) < 15, "seconds_since_picture": s.get("age"),
                          "recording": bool(s.get("recording")), "motion": (s.get("motion") or {}).get("active", False),
                          "sees": [d.get("name") or d.get("label") for d in ((s.get("ai") or {}).get("found") or [])]}
            if s.get("ptz"):
                cams[name]["position"] = {"pan": s["ptz"].get("x"), "tilt": s["ptz"].get("y")}
        return {"cameras": cams, "home_mode": home.get("home"), "ai_runs_on": usage.get("ai_device"),
                "guard_problems": usage.get("problems"), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_snapshot(camera: str, max_width: int = 1280) -> Image:
    """The current picture from one camera ('door', 'cam360', 'window'), as an image you can look at.
    Read-only (nothing is saved). Use bigeye_status first to see which cameras are live."""
    cams = _cameras()
    if camera not in cams:
        raise ValueError(f"unknown camera {camera!r}; cameras: {', '.join(cams)}")
    try:
        jpeg = _get(f"/{urllib.parse.quote(camera)}.jpg", raw=True)
        try:
            from PIL import Image as PILImage
            im = PILImage.open(io.BytesIO(jpeg)).convert("RGB")
            if im.width > max_width:
                im.thumbnail((max_width, max_width))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            jpeg = buf.getvalue()
        except Exception:
            pass
        return Image(data=jpeg, format="jpeg")
    except urllib.error.HTTPError as e:
        raise ValueError(f"{camera} has no picture right now ({e.code}); check bigeye_status") from e


@mcp.tool()
def bigeye_events(limit: int = 15, camera: str = "", query: str = "") -> dict:
    """Recent events (motion/person/animal) with the local AI's description of each picture.
    With `query` (plain words, e.g. 'someone at the front door', 'cat in the kitchen') the events are
    searched by meaning instead. Read-only."""
    try:
        if query.strip():
            events = _get("/search?" + urllib.parse.urlencode({"q": query}), timeout=90)
        else:
            events = _get("/events")
        if camera:
            events = [e for e in events if e.get("cam") == camera]
        out = []
        for e in events[: max(1, min(limit, 50))]:
            out.append({"time": time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("time", 0))), "camera": e.get("cam"),
                        "what": e.get("label"), "description": e.get("desc"), "score": e.get("score"),
                        "picture": e.get("picture"), "clip": e.get("clip")})
        return {"events": out, "count": len(out)}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_day_story(date: str = "") -> dict:
    """A short written summary of one day's events (YYYY-MM-DD, default today), written by the local AI.
    Read-only (it also saves the text as a report on the owner's PC)."""
    try:
        q = "?date=" + urllib.parse.quote(date) if date else ""
        return _get("/story" + q, timeout=240)
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_positions() -> dict:
    """The 360 camera's saved favourite positions and where it points now, and whether it is locked
    (locked = only the owner may move it). Read-only."""
    try:
        cam = next((n for n, s in _get("/state").items() if s.get("ptz")), None)
        if not cam:
            return {"error": "no pan/tilt camera"}
        info = _get(f"/ptz/{cam}/info")
        return {"camera": cam, "locked": info.get("locked", False), "now": {"pan": info.get("x"), "tilt": info.get("y")},
                "favourites": [p["name"] for p in info.get("presets", [])]}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_go_to(position: str) -> dict:
    """CHANGES SOMETHING: turns the 360 camera to one of its saved favourite positions (see bigeye_positions).
    Refused while the owner has locked the camera. Take a bigeye_snapshot a few seconds later to look."""
    try:
        pos = bigeye_positions()
        if pos.get("error"):
            return pos
        if pos.get("locked"):
            return {"refused": "the owner has locked the 360 camera (only manual control); ask the owner first"}
        if position not in pos["favourites"]:
            return {"error": f"no favourite {position!r}", "favourites": pos["favourites"]}
        r = _get(f"/ptz/{pos['camera']}/go?name=" + urllib.parse.quote(position))
        return {"ok": True, "camera": pos["camera"], "position": position, "pan": r.get("x"), "tilt": r.get("y")}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_set_home(home: bool) -> dict:
    """CHANGES SOMETHING: sets Home (True: nothing is recorded or sent) or Away (False: people are recorded and
    the owner can get phone messages). Only do this when the owner asked for it."""
    try:
        cur = _get("/home").get("home")
        if cur != home:
            cur = _get("/home", method="POST").get("home")
        return {"home_mode": cur}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_record(camera: str, start: bool = True) -> dict:
    """CHANGES SOMETHING: starts (start=True) or stops a recording on one camera; the file is saved on the
    owner's PC (Videos\\Sentinel-BigEye\\recordings). Only when the owner asked for it."""
    try:
        if camera not in _cameras():
            return {"error": f"unknown camera {camera!r}", "cameras": _cameras()}
        msg = _get(f"/rec/{urllib.parse.quote(camera)}?action={'start' if start else 'stop'}", raw=True).decode()
        return {"result": msg}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_usage() -> dict:
    """What BigEye uses of the PC (CPU, RAM, graphics memory), loaded AI models, and the guard's recent
    actions (it unloads models / slows the AI when the machine is overloaded). Read-only."""
    try:
        u = _get("/usage")
        return {k: u.get(k) for k in ("machine", "gpu", "bigeye", "bigeye_cpu", "models", "ai_device", "ai_ms", "ai_mode",
                                      "vision_ok", "problems", "actions")}
    except Exception as e:
        return _err(e)


@mcp.tool()
def bigeye_call_home() -> dict:
    """How often the cameras tried to phone their vendor clouds today (caught by the stand-in clouds; nothing
    left the house), per camera and per kind. Read-only."""
    try:
        d = _get("/telemetry/data?since=999999999999")
        return {"today": d.get("today"), "last_hour": d.get("hour"), "pictures_it_tried_to_upload": d.get("uploads"),
                "per_camera": d.get("cameras"), "per_kind": [{c["label"]: c["count"]} for c in d.get("categories", [])][:10]}
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run()
