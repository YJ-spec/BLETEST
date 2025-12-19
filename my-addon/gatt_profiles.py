# gatt_profiles.py
from typing import Optional

from profiles.zp2_profile import PROFILE as ZP2_PROFILE


def get_profile_by_adv_name(name: Optional[str]):
    """
    只用 BLE ADV name 判斷型號
    - 未來要支援 ZS2 就在這裡加 elif -> return ZS2_PROFILE
    """
    n = (name or "").strip().upper()
    if not n:
        return None

    # ZP2 判斷（未來你要放寬，就改這條）
    if "ZP2" in n:
        return ZP2_PROFILE

    # TODO: ZS2
    # if "ZS2" in n:
    #     return ZS2_PROFILE

    return None
