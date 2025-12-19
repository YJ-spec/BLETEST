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

from profile_store import (
    load_profiles,
    save_profiles,
    upsert_profile,
    delete_profile,
    set_current_profile,
)
from gatt_profiles import get_profile_by_adv_name


# ============================================================
# Banner-style block comments（你偏好的格式）
# ============================================================

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
# PROBE_PATH = os.path.join(DATA_DIR, "probe_results.json")

CONNECT_TIMEOUT_SEC = 12.0

# Scan cache
SCAN_CACHE_TTL_SEC = 300  # 5 minutes
SCAN_TIMEOUT_SEC = 8.0

# Profiles
PROFILE_FILE = "/data/profiles.json"
PROFILE_LOCK = asyncio.Lock()


# ------------------------------------------------------------
# FastAPI App
# ------------------------------------------------------------
app = FastAPI(title="BLE Lab (MVP-2: Probe + Fetch Details)")

# 靜態網頁（目前主要用 / 讀 index.html；/static 暫時可留）
app.mount("/static", StaticFiles(directory="/web"), name="static")


# ============================================================
# Utils: file I/O
# ============================================================
def ensure_data_dir() -> None:
    """Ensure /data exists."""
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(path: str, default: Any) -> Any:
    """Load JSON; return `default` on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj: Any) -> None:
    """Save JSON with pretty indent."""
    ensure_data_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ============================================================
# Utils: BLE scan cache helpers
# ============================================================
def find_adv_name_in_cache(address: str) -> str:
    """
    Find ADV name from scan cache by device address.

    注意：
    - 型號判斷只用 ADV name（你已拍板）
    - cache miss 時回傳空字串
    """
    cache = load_json(CACHE_PATH, default={"ts": 0, "results": []})
    for d in (cache.get("results") or []):
        if str(d.get("address")) == str(address):
            return str(d.get("name") or "")
    return ""


# ============================================================
# Utils: Bleak device properties
# ============================================================
def get_props(device) -> Dict[str, Any]:
    """
    Read bleak device.details.props if present.

    bleak 的 details/props 取決於平台與版本；這裡用安全方式取。
    """
    details = getattr(device, "details", None)
    if isinstance(details, dict):
        return details.get("props", {}) or {}
    return {}


def get_name(device, props: Dict[str, Any]) -> Optional[str]:
    """
    Prefer bleak device.name; fallback to props Name/Alias.
    """
    name = getattr(device, "name", None)
    if name:
        return name
    return props.get("Name") or props.get("Alias")


def get_rssi(props: Dict[str, Any]) -> Optional[int]:
    """RSSI may exist in props depending on platform/backend."""
    return props.get("RSSI")


def is_zp2_candidate(name: Optional[str], props: Dict[str, Any]) -> bool:
    """
    Legacy heuristic: detect ZP2 by substring.

    你目前保留此欄位（同時也有 model_key / is_supported）。
    """
    if name and "ZP2" in name.upper():
        return True

    alias = props.get("Alias")
    if isinstance(alias, str) and "ZP2" in alias.upper():
        return True

    return False


# ============================================================
# Utils: text/bytes encode & decode
# ============================================================
def _bytes_to_text(b: bytes) -> str:
    """Decode bytes to a clean UTF-8 string; strip NUL."""
    if not b:
        return ""

    try:
        return b.decode("utf-8", errors="replace").replace("\x00", "").strip()
    except Exception:
        return ""


def _encode_text(s: str) -> bytes:
    """Encode string as UTF-8 bytes (no NUL termination here)."""
    return (s or "").encode("utf-8")


# ============================================================
# BLE Scan
# ============================================================
async def do_scan() -> List[Dict[str, Any]]:
    """
    Scan BLE devices and produce cache-friendly list.

    - model 判斷：用 adv_name 丟給 get_profile_by_adv_name
    - 排序：ZP2 優先，其次 RSSI 強者優先
    """
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT_SEC)

    results: List[Dict[str, Any]] = []
    for d in devices:
        props = get_props(d)
        address = getattr(d, "address", None)
        name = get_name(d, props)
        rssi = get_rssi(props)

        profile = get_profile_by_adv_name(name)
        model_key = profile.key if profile else ""

        item = {
            "address": address,
            "name": name,
            "rssi": rssi,
            "address_type": props.get("AddressType"),
            "manufacturer_data": {
                str(k): (v.hex() if hasattr(v, "hex") else str(v))
                for k, v in (props.get("ManufacturerData") or {}).items()
            },
            "is_zp2": is_zp2_candidate(name, props),  # legacy heuristic
            "model_key": model_key,                   # e.g. "ZP2" / future "ZS2"
            "is_supported": bool(profile),            # whether profile exists
        }
        results.append(item)

    def rssi_sort(v: Dict[str, Any]) -> int:
        r = v.get("rssi")
        return r if isinstance(r, int) else -999

    results.sort(key=lambda x: (not x["is_zp2"], -rssi_sort(x)))
    return results


# ============================================================
# Probe (enumerate GATT)
# ============================================================
# async def probe_one(address: str) -> Dict[str, Any]:
#     """
#     Enumerate GATT services/characteristics for a single device.

#     用途：
#     - debug 廠商 GATT 定義
#     - future profile mapping 依據
#     """
#     result: Dict[str, Any] = {
#         "address": address,
#         "ok": False,
#         "error": None,
#         "services": [],
#     }

#     logging.info(f"[PROBE] connect -> {address}")

#     try:
#         async with BleakClient(address, timeout=CONNECT_TIMEOUT_SEC) as client:
#             logging.info(f"[PROBE] connected -> {address}")

#             svcs = client.services
#             if svcs is None:
#                 raise RuntimeError("client.services is None (GATT not resolved)")

#             service_list = list(getattr(svcs, "services", {}).values())
#             svc_count = len(service_list)
#             chr_count = sum(len(s.characteristics) for s in service_list)
#             logging.info(f"[PROBE] services -> {address}: svc={svc_count}, chr={chr_count}")

#             services_out: List[Dict[str, Any]] = []

#             for s in service_list:
#                 chars_out: List[Dict[str, Any]] = []

#                 for c in s.characteristics:
#                     descs: List[str] = []
#                     for d in (getattr(c, "descriptors", []) or []):
#                         try:
#                             descs.append(str(d.uuid))
#                         except Exception:
#                             descs.append(str(d))

#                     chars_out.append(
#                         {
#                             "uuid": str(c.uuid),
#                             "handle": getattr(c, "handle", None),
#                             "properties": list(getattr(c, "properties", []) or []),
#                             "descriptors": descs,
#                         }
#                     )

#                 services_out.append(
#                     {
#                         "uuid": str(s.uuid),
#                         "handle": getattr(s, "handle", None),
#                         "description": getattr(s, "description", None),
#                         "characteristics": chars_out,
#                     }
#                 )

#             result["services"] = services_out
#             result["ok"] = True

#     except Exception as e:
#         result["error"] = repr(e)
#         logging.warning(f"[PROBE] fail -> {address}: {repr(e)}")

#     return result


# ============================================================
# GATT helpers for fetch_details / write
# ============================================================
async def _read_in_service(client: BleakClient, service_uuid: str, char_uuid: str) -> bytes:
    """
    Read by (service_uuid, char_uuid) using characteristic object.

    規則（你已拍板）：
    - 不直接用 UUID string 讀寫
    - 必須先透過 service 取得 characteristic 物件
    """
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


async def _write_in_service(
    client: BleakClient,
    service_uuid: str,
    char_uuid: str,
    data: bytes,
    response: bool = True,
) -> None:
    """
    Write by (service_uuid, char_uuid) using characteristic object.

    關鍵：
    - 用「特定 characteristic 物件」寫，而不是 UUID 字串
    - 避免 characteristic UUID 重複導致寫錯目標
    """
    svcs = client.services
    if svcs is None:
        raise RuntimeError("client.services is None")

    svc = svcs.get_service(service_uuid)
    if svc is None:
        raise RuntimeError(f"service not found: {service_uuid}")

    ch = svc.get_characteristic(char_uuid)
    if ch is None:
        raise RuntimeError(f"char not found in {service_uuid}: {char_uuid}")

    await client.write_gatt_char(ch, data, response=response)


# ============================================================
# Routes: HTML
# ============================================================
@app.get("/", response_class=HTMLResponse)
def index():
    """Serve UI page."""
    with open("/web/index.html", "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# Routes: Profiles (profiles.json)
# ============================================================
class ProfileUpsertRequest(BaseModel):
    profile: Dict[str, Any]
    overwrite_id: Optional[str] = None


class ProfileSelectRequest(BaseModel):
    id: str


@app.get("/api/profiles")
async def api_profiles_get():
    """Get current profiles doc."""
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        return doc


@app.post("/api/profiles/upsert")
async def api_profiles_upsert(req: ProfileUpsertRequest):
    """Upsert a profile and persist it."""
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok, pid_or_err = upsert_profile(doc, req.profile, req.overwrite_id)
        if not ok:
            return {"ok": False, "error": pid_or_err}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "id": pid_or_err, "doc": doc}


@app.post("/api/profiles/select")
async def api_profiles_select(req: ProfileSelectRequest):
    """Set current_profile_id."""
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok, pid_or_err = set_current_profile(doc, req.id)
        if not ok:
            return {"ok": False, "error": pid_or_err}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "id": pid_or_err}


@app.delete("/api/profiles/{profile_id}")
async def api_profiles_delete(profile_id: str):
    """Delete a profile by id."""
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        ok = delete_profile(doc, profile_id)
        if not ok:
            return {"ok": False, "error": "Profile 不存在或 id 空"}
        save_profiles(doc, PROFILE_FILE)
        return {"ok": True, "doc": doc}


# ============================================================
# Routes: Scan + cache
# ============================================================
@app.post("/api/scan")
async def api_scan():
    """
    Scan BLE devices and save results to cache.

    回傳：
    - results: scan 即時結果
    - cache: 同時落盤到 /data/scan_cache.json
    """
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
    """
    Get devices from scan cache.

    - default only_zp2=True：符合你目前 UI/流程偏好
    - cache 超過 TTL：回傳 expired=True 並清空 cache
    """
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


# ============================================================
# Routes: Selected targets
# ============================================================
class ApplyBody(BaseModel):
    targets: List[str]


class FetchDetailsBody(BaseModel):
    targets: List[str]


class WriteProfileBody(BaseModel):
    targets: List[str]
    profile_id: str  # 由前端傳 profileSelect 的值


class CommandBody(BaseModel):
    targets: List[str]
    command: str  # "reset" or "reboot"


# @app.post("/api/apply")
# def api_apply(body: ApplyBody):
#     """Save selected target addresses for probe."""
#     selected = {
#         "ts": int(time.time()),
#         "targets": body.targets,
#     }
#     save_json(SELECTED_PATH, selected)
#     return {"ok": True, "saved": len(body.targets), "path": SELECTED_PATH}


# ============================================================
# Routes: fetch_details
# ============================================================
@app.post("/api/fetch_details")
async def api_fetch_details(body: FetchDetailsBody):
    """
    Fetch device details using GATT profile mapping.

    流程：
    - 由 cache 找 adv_name
    - 由 adv_name 找 gatt profile
    - 用 service + characteristic 物件做 read
    """
    results: List[Dict[str, Any]] = []

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
            "fw_version": "",
        }

        client: Optional[BleakClient] = None

        try:
            adv_name = find_adv_name_in_cache(address)
            profile = get_profile_by_adv_name(adv_name)
            if profile is None:
                raise RuntimeError(f"unsupported device by adv name: '{adv_name}'")

            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()

            # 保險：避免偶發 services 還沒 resolved
            await asyncio.sleep(0.2)

            ip_b = await _read_in_service(client, profile.SVC_SYS_INFO, profile.CH_IP)
            ssid_b = await _read_in_service(client, profile.SVC_WIFI_CFG, profile.CH_WIFI_COMBO)
            mode_b = await _read_in_service(client, profile.SVC_WIFI_CFG, profile.CH_MODE)
            mqtt_b = await _read_in_service(client, profile.SVC_WIFI_CFG, profile.CH_MQTT)
            model_b = await _read_in_service(client, profile.SVC_MODEL, profile.CH_MODEL)
            fw_b = await _read_in_service(client, profile.SVC_SYS_INFO, profile.CH_FW)

            ip_s = _bytes_to_text(ip_b)
            ssid_s = _bytes_to_text(ssid_b)
            mqtt_s = _bytes_to_text(mqtt_b)
            model_s = _bytes_to_text(model_b)
            fw_s = _bytes_to_text(fw_b)

            # mode 可能是 \x00/\x01，也可能是文字 "0"/"1"
            mode_v = ""
            if mode_b:
                if mode_b[:1] in (b"\x00", b"\x01"):
                    mode_v = "AWS" if mode_b[0] == 0 else "LOCAL"
                else:
                    t = _bytes_to_text(mode_b)
                    mode_v = "AWS" if t == "0" else ("LOCAL" if t == "1" else t)

            item.update(
                {
                    "ok": True,
                    "mode": mode_v,
                    "ssid": ssid_s,
                    "mqtt": mqtt_s,
                    "ip": ip_s,
                    "model": model_s,
                    "fw_version": fw_s,
                }
            )

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


# ============================================================
# Routes: write_profile
# ============================================================
@app.post("/api/write_profile")
async def api_write_profile(body: WriteProfileBody):
    """
    Write user_profile (profiles.json) into devices via gatt_profile.

    規則（已定案）：
    - user_profile 與 gatt_profile 變數名絕不混用
    - 每台 device 自己判斷 gatt_profile
    - 單台失敗不影響整批（但此函式目前保留原有 return 行為，不改）
    """
    targets = body.targets or []
    pid = (body.profile_id or "").strip()

    if not targets:
        return {"ok": False, "error": "targets is empty"}

    if not pid:
        return {"ok": False, "error": "profile_id is empty"}

    # ------------------------------
    # 1) 讀取「使用者設定 profile」
    # ------------------------------
    async with PROFILE_LOCK:
        doc = load_profiles(PROFILE_FILE)
        plist = doc.get("profiles", []) or []
        user_profile = next((p for p in plist if str(p.get("id", "")) == pid), None)

    if not user_profile:
        return {"ok": False, "error": f"profile not found: {pid}"}

    mode = (user_profile.get("mode") or "").strip().upper()
    ssid = (user_profile.get("ssid") or "").strip()
    password = (user_profile.get("password") or "").strip()
    mqtt_in = (user_profile.get("mqtt") or "").strip()

    results: List[Dict[str, Any]] = []

    for address in targets:
        item = {"address": address, "ok": False, "error": None}
        client: Optional[BleakClient] = None

        try:
            adv_name = find_adv_name_in_cache(address)

            # 由 ADV name 判斷型號對應的 gatt profile（profile 驅動）
            gatt_profile = get_profile_by_adv_name(adv_name)
            if gatt_profile is None:
                raise RuntimeError(f"unsupported device by adv name: '{adv_name}'")

            # 批次固定：mode / wifi payload
            # 每台計算：mqtt_text（由 gatt_profile 決定 encode 行為）
            mqtt_text = gatt_profile.build_mqtt_text(mqtt_in)
            mode_text = "0" if mode == "AWS" else "1"
            wifi_payload = _encode_text(ssid) + b"\x00" + _encode_text(password)

            if not mqtt_text:
                # ⚠️ 保留原本行為：遇到空 mqtt 直接 return（不做功能修改）
                return {"ok": False, "error": "profile.mqtt is empty"}

            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()

            # 保險：避免偶發 services 還沒 resolved
            await asyncio.sleep(0.2)

            await _write_in_service(
                client,
                gatt_profile.SVC_WIFI_CFG,
                gatt_profile.CH_MODE,
                _encode_text(mode_text),
                response=True,
            )
            await asyncio.sleep(0.15)

            await _write_in_service(
                client,
                gatt_profile.SVC_WIFI_CFG,
                gatt_profile.CH_MQTT,
                _encode_text(mqtt_text),
                response=True,
            )
            await asyncio.sleep(0.15)

            await _write_in_service(
                client,
                gatt_profile.SVC_WIFI_CFG,
                gatt_profile.CH_WIFI_COMBO,
                wifi_payload,
                response=True,
            )
            await asyncio.sleep(0.15)

            item["ok"] = True

        except Exception as e:
            item["error"] = repr(e)

        finally:
            try:
                if client is not None and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

        results.append(item)

    return {
        "ok": True,
        "profile_id": pid,
        "mode_text": mode_text,
        "mqtt_text": mqtt_text,
        "results": results,
    }


# ============================================================
# Routes: send command (reset / reboot)
# ============================================================
@app.post("/api/send_command")
async def api_send_command(body: CommandBody):
    """
    Send a simple command to devices.

    注意：
    - 目前 reboot 無效，reset 有效（你已記錄）
    - 仍然使用 service + characteristic 物件寫入，避免 UUID 重複問題
    """
    cmd = (body.command or "").strip().lower()
    if cmd not in ("reset", "reboot"):
        return {"ok": False, "error": "invalid command"}

    targets = body.targets or []
    if not targets:
        return {"ok": False, "error": "targets is empty"}

    payload = cmd.encode("utf-8")
    results: List[Dict[str, Any]] = []

    for address in targets:
        item = {"address": address, "ok": False, "error": None}
        client = None

        try:
            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()
            await asyncio.sleep(0.2)

            adv_name = find_adv_name_in_cache(address)
            profile = get_profile_by_adv_name(adv_name)
            if profile is None:
                raise RuntimeError(f"unsupported device by adv name: '{adv_name}'")

            await _write_in_service(
                client,
                profile.SVC_WIFI_CFG,
                profile.CH_COMMAND,
                payload,
                response=True,
            )

            item["ok"] = True

        except Exception as e:
            item["error"] = repr(e)

        finally:
            try:
                if client and client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

        results.append(item)

    return {
        "ok": True,
        "command": cmd,
        "results": results,
    }


# ============================================================
# Routes: probe selected targets
# ============================================================
# @app.post("/api/probe")
# async def api_probe():
#     """Probe all targets saved by /api/apply and persist results to file."""
#     selected = load_json(SELECTED_PATH, default={"targets": []})
#     targets = selected.get("targets") or []

#     out = {
#         "ts": int(time.time()),
#         "count": len(targets),
#         "results": [],
#     }

#     for addr in targets:
#         out["results"].append(await probe_one(addr))

#     save_json(PROBE_PATH, out)
#     return {"ok": True, "count": out["count"]}


# @app.get("/api/probe_result")
# def api_probe_result():
#     """Read persisted probe results."""
#     return load_json(PROBE_PATH, default={"ts": 0, "count": 0, "results": []})


# ============================================================
# Entrypoint
# ============================================================
def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
