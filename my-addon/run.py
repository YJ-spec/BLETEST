import os
import json
import time
import asyncio
import logging
from typing import List, Dict, Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient
from profile_store import load_profiles, save_profiles, upsert_profile, delete_profile, set_current_profile

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ------------------------------------------------------------
# Paths / Constants
# ------------------------------------------------------------
DATA_DIR = "/data"
CACHE_PATH = os.path.join(DATA_DIR, "scan_cache.json")
SELECTED_PATH = os.path.join(DATA_DIR, "selected_devices.json")
PROBE_PATH = os.path.join(DATA_DIR, "probe_results.json")

CONNECT_TIMEOUT_SEC = 12.0
SCAN_CACHE_TTL_SEC = 300  # 5 min
SCAN_TIMEOUT_SEC = 8.0

app = FastAPI(title="BLE Lab (MVP-2: Probe + Fetch Details)")
PROFILE_LOCK = asyncio.Lock()
PROFILE_FILE = "/data/profiles.json"


# 靜態網頁（目前主要用 / 讀 index.html，/static 暫時可留）
app.mount("/static", StaticFiles(directory="/web"), name="static")


# ------------------------------------------------------------
# Utils
# ------------------------------------------------------------
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj):
    ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def get_props(device) -> Dict[str, Any]:
    details = getattr(device, "details", None)
    if isinstance(details, dict):
        return details.get("props", {}) or {}
    return {}


def get_name(device, props: Dict[str, Any]) -> Optional[str]:
    name = getattr(device, "name", None)
    if name:
        return name
    return props.get("Name") or props.get("Alias")


def get_rssi(props: Dict[str, Any]) -> Optional[int]:
    return props.get("RSSI")


def is_zp2_candidate(name: Optional[str], props: Dict[str, Any]) -> bool:
    if name and "ZP2" in name.upper():
        return True
    alias = props.get("Alias")
    if isinstance(alias, str) and "ZP2" in alias.upper():
        return True
    return False


def _bytes_to_text(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8", errors="replace").replace("\x00", "").strip()
    except Exception:
        return ""


async def do_scan() -> List[Dict[str, Any]]:
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT_SEC)

    results: List[Dict[str, Any]] = []
    for d in devices:
        props = get_props(d)
        address = getattr(d, "address", None)
        name = get_name(d, props)
        rssi = get_rssi(props)

        item = {
            "address": address,
            "name": name,
            "rssi": rssi,
            "address_type": props.get("AddressType"),
            "manufacturer_data": {
                str(k): (v.hex() if hasattr(v, "hex") else str(v))
                for k, v in (props.get("ManufacturerData") or {}).items()
            },
            "is_zp2": is_zp2_candidate(name, props),
        }
        results.append(item)

    def rssi_sort(v):
        r = v.get("rssi")
        return r if isinstance(r, int) else -999

    results.sort(key=lambda x: (not x["is_zp2"], -rssi_sort(x)))
    return results


# ------------------------------------------------------------
# Probe (enumerate GATT)
# ------------------------------------------------------------
async def probe_one(address: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "address": address,
        "ok": False,
        "error": None,
        "services": [],
    }

    logging.info(f"[PROBE] connect -> {address}")

    try:
        async with BleakClient(address, timeout=CONNECT_TIMEOUT_SEC) as client:
            logging.info(f"[PROBE] connected -> {address}")

            svcs = client.services
            if svcs is None:
                raise RuntimeError("client.services is None (GATT not resolved)")

            service_list = list(getattr(svcs, "services", {}).values())
            svc_count = len(service_list)
            chr_count = sum(len(s.characteristics) for s in service_list)
            logging.info(f"[PROBE] services -> {address}: svc={svc_count}, chr={chr_count}")

            services_out: List[Dict[str, Any]] = []

            for s in service_list:
                chars_out: List[Dict[str, Any]] = []
                for c in s.characteristics:
                    descs = []
                    for d in (getattr(c, "descriptors", []) or []):
                        try:
                            descs.append(str(d.uuid))
                        except Exception:
                            descs.append(str(d))

                    chars_out.append({
                        "uuid": str(c.uuid),
                        "handle": getattr(c, "handle", None),
                        "properties": list(getattr(c, "properties", []) or []),
                        "descriptors": descs,
                    })

                services_out.append({
                    "uuid": str(s.uuid),
                    "handle": getattr(s, "handle", None),
                    "description": getattr(s, "description", None),
                    "characteristics": chars_out,
                })

            result["services"] = services_out
            result["ok"] = True

    except Exception as e:
        result["error"] = repr(e)
        logging.warning(f"[PROBE] fail -> {address}: {repr(e)}")

    return result


# ------------------------------------------------------------
# Read specific (service, char) helper for fetch_details
# ------------------------------------------------------------
async def _read_in_service(client: BleakClient, service_uuid: str, char_uuid: str) -> bytes:
    svcs = client.services
    if svcs is None:
        raise RuntimeError("client.services is None")

    svc = svcs.get_service(service_uuid)
    if svc is None:
        raise RuntimeError(f"service not found: {service_uuid}")

    ch = svc.get_characteristic(char_uuid)
    if ch is None:
        raise RuntimeError(f"char not found in {service_uuid}: {char_uuid}")

    return await client.read_gatt_char(ch)


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open("/web/index.html", "r", encoding="utf-8") as f:
        return f.read()
class ProfileUpsertRequest(BaseModel):
    profile: Dict[str, Any]
    overwrite_id: Optional[str] = None

class ProfileSelectRequest(BaseModel):
    id: str

@app.get("/api/profiles")
async def api_profiles_get():
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        return doc

@app.post("/api/profiles/upsert")
async def api_profiles_upsert(req: ProfileUpsertRequest):
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok, pid_or_err = upsert_profile(doc, req.profile, req.overwrite_id)
        if not ok:
            return {"ok": False, "error": pid_or_err}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "id": pid_or_err, "doc": doc}

@app.post("/api/profiles/select")
async def api_profiles_select(req: ProfileSelectRequest):
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok, pid_or_err = set_current_profile(doc, req.id)
        if not ok:
            return {"ok": False, "error": pid_or_err}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "id": pid_or_err}

@app.delete("/api/profiles/{profile_id}")
async def api_profiles_delete(profile_id: str):
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok = delete_profile(doc, profile_id)
        if not ok:
            return {"ok": False, "error": "Profile 不存在或 id 空"}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "doc": doc}


@app.post("/api/scan")
async def api_scan():
    results = await do_scan()
    payload = {
        "ts": int(time.time()),
        "timeout_sec": SCAN_TIMEOUT_SEC,
        "results": results,
    }
    save_json(CACHE_PATH, payload)
    return {
        "ok": True,
        "count": len(results),
        "zp2_count": sum(1 for r in results if r["is_zp2"]),
        "results": results,
    }



@app.get("/api/devices")
def api_devices(only_zp2: bool = True):
    cache = load_json(CACHE_PATH, default={"ts": 0, "results": []})
    ts = int(cache.get("ts", 0) or 0)
    age = int(time.time()) - ts

    if ts == 0 or age > SCAN_CACHE_TTL_SEC:
        save_json(CACHE_PATH, {"ts": 0, "timeout_sec": SCAN_TIMEOUT_SEC, "results": []})
        return {
            "ok": True,
            "age_sec": age,
            "ttl_sec": SCAN_CACHE_TTL_SEC,
            "expired": True,
            "devices": [],
        }

    results = cache.get("results", [])
    if only_zp2:
        results = [r for r in results if r.get("is_zp2")]

    return {
        "ok": True,
        "age_sec": age,
        "ttl_sec": SCAN_CACHE_TTL_SEC,
        "expired": False,
        "devices": results,
    }


class ApplyBody(BaseModel):
    targets: List[str]


@app.post("/api/apply")
def api_apply(body: ApplyBody):
    selected = {
        "ts": int(time.time()),
        "targets": body.targets,
    }
    save_json(SELECTED_PATH, selected)
    return {"ok": True, "saved": len(body.targets), "path": SELECTED_PATH}


class FetchDetailsBody(BaseModel):
    targets: List[str]


@app.post("/api/fetch_details")
async def api_fetch_details(body: FetchDetailsBody):
    # UUID mapping (128-bit)
    SVC_120A = "0000120a-0000-1000-8000-00805f9b34fb"
    CH_IP = "00002a26-0000-1000-8000-00805f9b34fb"

    SVC_12AA = "000012aa-0000-1000-8000-00805f9b34fb"
    CH_SSID = "000012a1-0000-1000-8000-00805f9b34fb"
    CH_MODE = "000012a5-0000-1000-8000-00805f9b34fb"
    CH_MQTT = "000012a6-0000-1000-8000-00805f9b34fb"

    SVC_12C0 = "000012c0-0000-1000-8000-00805f9b34fb"
    CH_MODEL = "00000000-0000-1000-8000-00805f9b34fb"

    results = []

    for address in body.targets:
        item = {
            "address": address,
            "ok": False,
            "error": None,
            "mode": "",
            "ssid": "",
            "mqtt": "",
            "ip": "",
            "model": "",
        }

        client: Optional[BleakClient] = None
        try:
            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()

            # 保險：避免偶發 services 還沒 resolved
            await asyncio.sleep(0.2)

            ip_b = await _read_in_service(client, SVC_120A, CH_IP)
            ssid_b = await _read_in_service(client, SVC_12AA, CH_SSID)
            mode_b = await _read_in_service(client, SVC_12AA, CH_MODE)
            mqtt_b = await _read_in_service(client, SVC_12AA, CH_MQTT)
            model_b = await _read_in_service(client, SVC_12C0, CH_MODEL)

            ip_s = _bytes_to_text(ip_b)
            ssid_s = _bytes_to_text(ssid_b)
            mqtt_s = _bytes_to_text(mqtt_b)
            model_s = _bytes_to_text(model_b)

            mode_v = ""
            if mode_b:
                if mode_b[:1] in (b"\x00", b"\x01"):
                    mode_v = "AWS" if mode_b[0] == 0 else "LOCAL"
                else:
                    t = _bytes_to_text(mode_b)
                    mode_v = "AWS" if t == "0" else ("LOCAL" if t == "1" else t)

            item.update({
                "ok": True,
                "mode": mode_v,
                "ssid": ssid_s,
                "mqtt": mqtt_s,
                "ip": ip_s,
                "model": model_s,
            })

        except Exception as e:
            item["error"] = repr(e)

        finally:
            try:
                if client is not None and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

        results.append(item)

    return {"ok": True, "results": results}


@app.post("/api/probe")
async def api_probe():
    selected = load_json(SELECTED_PATH, default={"targets": []})
    targets = selected.get("targets") or []

    out = {
        "ts": int(time.time()),
        "count": len(targets),
        "results": [],
    }

    for addr in targets:
        out["results"].append(await probe_one(addr))

    save_json(PROBE_PATH, out)
    return {"ok": True, "count": out["count"]}


@app.get("/api/probe_result")
def api_probe_result():
    return load_json(PROBE_PATH, default={"ts": 0, "count": 0, "results": []})


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
