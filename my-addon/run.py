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

# ============================================================
# Routes: fetch_details
# ============================================================
@app.post("/api/fetch_details")
async def api_fetch_details(body: FetchDetailsBody):
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

            # --- READ raw ---
            ep = profile.EP_IP
            ip_b = await _read_in_service(client, ep.service, ep.char)

            ep = profile.EP_WIFI_COMBO
            ssid_b = await _read_in_service(client, ep.service, ep.char)

            ep = profile.EP_MODE
            mode_b = await _read_in_service(client, ep.service, ep.char)

            ep = profile.EP_MQTT
            mqtt_b = await _read_in_service(client, ep.service, ep.char)

            ep = profile.EP_MODEL
            model_b = await _read_in_service(client, ep.service, ep.char)

            ep = profile.EP_FW_VERSION
            fw_b = await _read_in_service(client, ep.service, ep.char)

            # --- DECODE semantic ---
            ip_s = profile.decode_ip(ip_b)
            # 這顆其實是 combo（ssid\0pwd），你原本也直接當字串丟回去，先保留同樣行為
            ssid_s = profile.decode_text(ssid_b)
            mqtt_s = profile.decode_mqtt(mqtt_b)
            model_s = profile.decode_model(model_b)
            fw_s = profile.decode_fw_version(fw_b)
            mode_v = profile.decode_mode(mode_b)

            item.update({
                "ok": True,
                "mode": mode_v,
                "ssid": ssid_s,
                "mqtt": mqtt_s,
                "ip": ip_s,
                "model": model_s,
                "fw_version": fw_s,
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



# ============================================================
# Routes: write_profile
# ============================================================
@app.post("/api/write_profile")
async def api_write_profile(body: WriteProfileBody):
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

    results = []

    # 這兩個給 return 用（沿用你原本結構：回傳最後一台的值）
    mode_text = ""
    mqtt_text = ""

    for address in targets:
        item = {"address": address, "ok": False, "error": None}
        client: Optional[BleakClient] = None

        try:
            adv_name = find_adv_name_in_cache(address)
            profile = get_profile_by_adv_name(adv_name)
            if profile is None:
                raise RuntimeError(f"unsupported device by adv name: '{adv_name}'")

            # profile 負責規格一致性（文字/拼接）
            mqtt_text = profile.encode_mqtt(mqtt_in)      # str
            mode_text = profile.encode_mode(mode)         # str
            wifi_payload = profile.encode_wifi_combo(ssid, password)  # bytes

            # ⚠️ 保留原本行為：mqtt 空就直接 return（不改策略）
            if not mqtt_text:
                return {"ok": False, "error": "profile.mqtt is empty"}

            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()
            await asyncio.sleep(0.2)

            # # MODE（文字 -> bytes）
            # ep = profile.EP_MODE
            # await _write_in_service(
            #     client, ep.service, ep.char,
            #     mqtt_text=None,  # placeholder, ignore
            # )
            # # ↑ 這行不要，下面是正確的（我留這個註解避免你 copy 錯）

            ep = profile.EP_MODE
            await _write_in_service(
                client,
                ep.service,
                ep.char,
                _encode_text(mode_text),
                response=True
            )
            await asyncio.sleep(0.15)

            # MQTT（文字 -> bytes）
            ep = profile.EP_MQTT
            await _write_in_service(
                client,
                ep.service,
                ep.char,
                _encode_text(mqtt_text),
                response=True
            )
            await asyncio.sleep(0.15)

            # WIFI_COMBO（已是 bytes）
            ep = profile.EP_WIFI_COMBO
            await _write_in_service(
                client,
                ep.service,
                ep.char,
                wifi_payload,
                response=True
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
    cmd = (body.command or "").strip().lower()
    if cmd not in ("reset", "reboot"):
        return {"ok": False, "error": "invalid command"}

    targets = body.targets or []
    if not targets:
        return {"ok": False, "error": "targets is empty"}

    results = []

    for address in targets:
        item = {"address": address, "ok": False, "error": None}
        client: Optional[BleakClient] = None

        try:
            client = BleakClient(address, timeout=CONNECT_TIMEOUT_SEC)
            await client.connect()
            await asyncio.sleep(0.2)

            adv_name = find_adv_name_in_cache(address)
            profile = get_profile_by_adv_name(adv_name)
            if profile is None:
                raise RuntimeError(f"unsupported device by adv name: '{adv_name}'")

            cmd_text = profile.encode_command(cmd)  # str
            payload = _encode_text(cmd_text)        # bytes

            ep = profile.EP_COMMAND
            await _write_in_service(
                client,
                ep.service,
                ep.char,
                payload,
                response=True
            )

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

    return {"ok": True, "command": cmd, "results": results}


# ============================================================
# Entrypoint
# ============================================================
def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
