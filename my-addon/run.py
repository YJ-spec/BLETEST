import os
import json
import time
import asyncio
from typing import List, Dict, Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


DATA_DIR = "/data"
CACHE_PATH = os.path.join(DATA_DIR, "scan_cache.json")
SELECTED_PATH = os.path.join(DATA_DIR, "selected_devices.json")
PROBE_PATH = os.path.join(DATA_DIR, "probe_results.json")

CONNECT_TIMEOUT_SEC = 12.0
SCAN_CACHE_TTL_SEC = 60  # 掃描結果保存 60 秒
SCAN_TIMEOUT_SEC = 8.0   # 實際掃描時間

app = FastAPI(title="BLE Lab (MVP-2: Probe GATT)")

# 掛靜態網頁
app.mount("/static", StaticFiles(directory="/web"), name="static")


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
    # bleak 在 Linux/BlueZ 後端通常把資訊塞在 details["props"]
    details = getattr(device, "details", None)
    if isinstance(details, dict):
        return details.get("props", {}) or {}
    return {}


def get_name(device, props: Dict[str, Any]) -> Optional[str]:
    name = getattr(device, "name", None)
    if name:
        return name
    # BlueZ props 可能有 Name 或 Alias
    return props.get("Name") or props.get("Alias")


def get_rssi(props: Dict[str, Any]) -> Optional[int]:
    return props.get("RSSI")


def is_zp2_candidate(name: Optional[str], props: Dict[str, Any]) -> bool:
    # MVP-1：先用「名字/別名包含 ZP2」做初版篩選（之後再改成 ManufacturerData matcher）
    if name and "ZP2" in name.upper():
        return True
    alias = props.get("Alias")
    if isinstance(alias, str) and "ZP2" in alias.upper():
        return True
    return False


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
            "manufacturer_data": {str(k): (v.hex() if hasattr(v, "hex") else str(v))
                                  for k, v in (props.get("ManufacturerData") or {}).items()},
            "is_zp2": is_zp2_candidate(name, props),
        }
        results.append(item)

    # 排序：ZP2 在前、再看 RSSI 強弱
    def rssi_sort(v):
        r = v.get("rssi")
        return r if isinstance(r, int) else -999

    results.sort(key=lambda x: (not x["is_zp2"], -rssi_sort(x)))
    return results

import logging
from typing import Dict, Any, List

# 連線超時：你可以調大一點，例如 20.0
CONNECT_TIMEOUT_SEC = 12.0

# 建議在檔案最上面只設定一次 logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def probe_one(address: str) -> Dict[str, Any]:
    """
    逐台裝置：
    1) BLE connect
    2) get_services()
    3) 把 services/characteristics/properties 整理成 JSON-friendly 結構回傳

    注意：
    - 這裡「一定有連線」，拿 GATT 必須連線。
    - 失敗會回 ok=False 並提供 error（用 repr(e) 避免空字串）。
    """
    result: Dict[str, Any] = {
        "address": address,
        "ok": False,
        "error": None,
        "services": [],  # List[service]
    }

    logging.info(f"[PROBE] connect -> {address}")

    try:
        # BleakClient 會透過 BlueZ (DBus) 連線
        async with BleakClient(address, timeout=CONNECT_TIMEOUT_SEC) as client:
            logging.info(f"[PROBE] connected -> {address}")

            svcs = await client.get_services()
            logging.info(f"[PROBE] services_count -> {address}: {len(svcs)}")

            services_out: List[Dict[str, Any]] = []

            for s in svcs:
                chars_out: List[Dict[str, Any]] = []

                for c in s.characteristics:
                    # descriptors 在 bleak/bluez 有時候不一定完整，但能列就列
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
        # str(e) 有時會是空字串，repr(e) 才看得到類型
        result["error"] = repr(e)
        logging.warning(f"[PROBE] fail -> {address}: {repr(e)}")

    return result


@app.get("/", response_class=HTMLResponse)
def index():
    # 直接回傳 web/index.html
    with open("/web/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/scan")
async def api_scan():
    results = await do_scan()
    payload = {
        "ts": int(time.time()),
        "timeout_sec": SCAN_TIMEOUT_SEC,
        "results": results,
    }
    save_json(CACHE_PATH, payload)
    return {"ok": True, "count": len(results), "zp2_count": sum(1 for r in results if r["is_zp2"])}


@app.get("/api/devices")
def api_devices(only_zp2: bool = True):
    cache = load_json(CACHE_PATH, default={"ts": 0, "results": []})
    age = int(time.time()) - int(cache.get("ts", 0) or 0)

    results = cache.get("results", [])
    if only_zp2:
        results = [r for r in results if r.get("is_zp2")]

    return {
        "ok": True,
        "age_sec": age,
        "ttl_sec": SCAN_CACHE_TTL_SEC,
        "devices": results,
    }


class ApplyBody(BaseModel):
    targets: List[str]  # address list (MAC)


@app.post("/api/apply")
def api_apply(body: ApplyBody):
    # MVP-1：只存檔，不做 BLE 寫入
    selected = {
        "ts": int(time.time()),
        "targets": body.targets,
    }
    save_json(SELECTED_PATH, selected)
    return {"ok": True, "saved": len(body.targets), "path": SELECTED_PATH}


@app.post("/api/probe")
async def api_probe():
    """
    MVP-2: 讀取 /data/selected_devices.json 裡的 targets
    逐台 connect + enumerate services/characteristics
    """
    selected = load_json(SELECTED_PATH, default={"targets": []})
    targets = selected.get("targets") or []

    out = {
        "ts": int(time.time()),
        "count": len(targets),
        "results": [],
    }

    # 逐台 probe（先不要並行）
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
