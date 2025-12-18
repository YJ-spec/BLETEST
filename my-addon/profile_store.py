# profile_store.py
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_PROFILE_FILE = "/data/profiles.json"
SCHEMA_VERSION = 1

def _now_ms() -> int:
    return int(time.time() * 1000)

def _atomic_write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _default_doc() -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "current_profile_id": "",
        "profiles": []
    }

def load_profiles(path: str = DEFAULT_PROFILE_FILE) -> Dict[str, Any]:
    if not os.path.exists(path):
        doc = _default_doc()
        _atomic_write_json(path, doc)
        return doc

    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        # 檔案壞掉：保底重建，避免整個服務掛掉
        doc = _default_doc()
        _atomic_write_json(path, doc)
        return doc

    if not isinstance(doc, dict):
        doc = _default_doc()

    doc.setdefault("schema", SCHEMA_VERSION)
    doc.setdefault("current_profile_id", "")
    doc.setdefault("profiles", [])

    if not isinstance(doc["profiles"], list):
        doc["profiles"] = []

    return doc

def save_profiles(doc: Dict[str, Any], path: str = DEFAULT_PROFILE_FILE) -> None:
    # 固定 key，避免亂長
    out = {
        "schema": SCHEMA_VERSION,
        "current_profile_id": str(doc.get("current_profile_id", "") or ""),
        "profiles": doc.get("profiles", []) or []
    }
    _atomic_write_json(path, out)

def normalize_profile(p: Dict[str, Any]) -> Dict[str, Any]:
    # 固定 profile 格式（你之前拍板的那套欄位）
    pid = str(p.get("id", "") or "").strip()
    name = str(p.get("name", "") or "").strip()
    mode = str(p.get("mode", "LOCAL") or "LOCAL").strip().upper()
    if mode not in ("LOCAL", "AWS"):
        mode = "LOCAL"

    ssid = str(p.get("ssid", "") or "").strip()
    password = str(p.get("password", "") or "").strip()
    mqtt = str(p.get("mqtt", "") or "").strip()

    updated_at = int(p.get("updated_at") or _now_ms())

    return {
        "id": pid,
        "name": name,
        "mode": mode,
        "ssid": ssid,
        "password": password,
        "mqtt": mqtt,
        "updated_at": updated_at
    }

def upsert_profile(doc: Dict[str, Any], profile: Dict[str, Any], overwrite_id: Optional[str] = None) -> Tuple[bool, str]:
    p = normalize_profile(profile)
    pid = (overwrite_id or p["id"]).strip()

    if not pid:
        return False, "Profile ID 不能為空"

    profiles: List[Dict[str, Any]] = doc.get("profiles", [])
    idx = next((i for i, x in enumerate(profiles) if str(x.get("id", "")) == pid), -1)

    p["id"] = pid
    p["updated_at"] = _now_ms()

    if idx >= 0:
        profiles[idx] = p
    else:
        profiles.append(p)

    doc["profiles"] = profiles
    doc["current_profile_id"] = pid
    return True, pid

def delete_profile(doc: Dict[str, Any], pid: str) -> bool:
    pid = str(pid or "").strip()
    if not pid:
        return False

    before = len(doc.get("profiles", []))
    doc["profiles"] = [p for p in doc.get("profiles", []) if str(p.get("id", "")) != pid]
    after = len(doc.get("profiles", []))

    if doc.get("current_profile_id") == pid:
        doc["current_profile_id"] = ""

    return after != before

def set_current_profile(doc: Dict[str, Any], pid: str) -> Tuple[bool, str]:
    pid = str(pid or "").strip()
    if not pid:
        doc["current_profile_id"] = ""
        return True, ""

    exists = any(str(p.get("id", "")) == pid for p in doc.get("profiles", []))
    if not exists:
        return False, "Profile 不存在"

    doc["current_profile_id"] = pid
    return True, pid
