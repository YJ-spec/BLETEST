# profile_store.py
"""
Profile Store
-------------
負責 profiles.json 的讀寫與基本資料整形（normalize）。

設計目標：
- 檔案壞掉也不讓服務掛掉（保底重建）
- atomic write，避免寫到一半斷電導致 JSON 半套
- 固定 schema / key 排列，避免資料越存越亂
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# Banner-style block comments（你偏好的格式）
# ============================================================

# ------------------------------------------------------------
# 常數與預設
# ------------------------------------------------------------
DEFAULT_PROFILE_FILE = "/data/profiles.json"
SCHEMA_VERSION = 1


# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------
def _now_ms() -> int:
    """Return current time in milliseconds (int)."""
    return int(time.time() * 1000)


def _atomic_write_json(path: str, data: Any) -> None:
    """
    Atomic write JSON to `path`.

    作法：
    1) 寫到同資料夾的 tmp 檔
    2) 使用 os.replace 直接取代（atomic on most filesystems）
    """
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _default_doc() -> Dict[str, Any]:
    """Return a new default document structure."""
    return {
        "schema": SCHEMA_VERSION,
        "current_profile_id": "",
        "profiles": [],
    }


# ------------------------------------------------------------
# Public API: load / save
# ------------------------------------------------------------
def load_profiles(path: str = DEFAULT_PROFILE_FILE) -> Dict[str, Any]:
    """
    Load profiles document from JSON file.

    保底策略：
    - 檔案不存在：建立一份 default doc 並落盤
    - JSON 壞掉或讀取失敗：重建檔案（避免整個服務掛掉）
    - 結構不是 dict / profiles 不是 list：強制修正成可用格式
    """
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

    # 結構防呆：確保最終回傳一定是 dict
    if not isinstance(doc, dict):
        doc = _default_doc()

    # 補齊必要欄位
    doc.setdefault("schema", SCHEMA_VERSION)
    doc.setdefault("current_profile_id", "")
    doc.setdefault("profiles", [])

    # profiles 型別防呆
    if not isinstance(doc["profiles"], list):
        doc["profiles"] = []

    return doc


def save_profiles(doc: Dict[str, Any], path: str = DEFAULT_PROFILE_FILE) -> None:
    """
    Save profiles document to JSON file.

    注意：
    - 這裡刻意固定 key 順序與內容，避免外部亂塞其它 key 造成檔案膨脹
    - schema 永遠以本程式定義版本為準
    """
    out = {
        "schema": SCHEMA_VERSION,
        "current_profile_id": str(doc.get("current_profile_id", "") or ""),
        "profiles": doc.get("profiles", []) or [],
    }
    _atomic_write_json(path, out)


# ------------------------------------------------------------
# Profile normalization
# ------------------------------------------------------------
def normalize_profile(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a profile dict into the canonical shape.

    Canonical fields（你拍板的欄位）：
    - id, name, mode, ssid, password, mqtt, updated_at

    mode 僅允許：
    - LOCAL
    - AWS
    """
    pid = str(p.get("id", "") or "").strip()
    name = str(p.get("name", "") or "").strip()

    mode = str(p.get("mode", "LOCAL") or "LOCAL").strip().upper()
    if mode not in ("LOCAL", "AWS"):
        mode = "LOCAL"

    ssid = str(p.get("ssid", "") or "").strip()
    password = str(p.get("password", "") or "").strip()
    mqtt = str(p.get("mqtt", "") or "").strip()

    # 若傳入沒有 updated_at，採用現在時間；若有則盡量轉成 int
    updated_at = int(p.get("updated_at") or _now_ms())

    return {
        "id": pid,
        "name": name,
        "mode": mode,
        "ssid": ssid,
        "password": password,
        "mqtt": mqtt,
        "updated_at": updated_at,
    }


# ------------------------------------------------------------
# Public API: CRUD helpers
# ------------------------------------------------------------
def upsert_profile(
    doc: Dict[str, Any],
    profile: Dict[str, Any],
    overwrite_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Insert or update a profile.

    行為：
    - normalize 後寫入
    - 若 overwrite_id 有給：以 overwrite_id 為準（用於「另存/覆蓋」的情境）
    - upsert 成功會把 current_profile_id 一起指向該 profile
    """
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
    """
    Delete a profile by id.

    注意：
    - 如果刪掉的是 current_profile_id，會一併清空 current_profile_id
    """
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
    """
    Set current profile id.

    規則：
    - pid 空字串：視為「取消選取」，回傳 (True, "")
    - pid 不存在：回傳 (False, "Profile 不存在")
    """
    pid = str(pid or "").strip()
    if not pid:
        doc["current_profile_id"] = ""
        return True, ""

    exists = any(str(p.get("id", "")) == pid for p in doc.get("profiles", []))
    if not exists:
        return False, "Profile 不存在"

    doc["current_profile_id"] = pid
    return True, pid
