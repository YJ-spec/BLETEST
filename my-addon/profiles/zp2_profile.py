# # profiles/zp2_profile.py
# from dataclasses import dataclass
# from typing import Dict


# @dataclass(frozen=True)
# class Zp2GattProfile:
#     """
#     ZP2 GATT mapping (Source of Truth)

#     注意：
#     - UUID 以 service+char 定位（避免別的 service 出現重複 UUID）
#     - payload 規則照你已驗證的規格
#     """
#     key: str = "ZP2"

#     # ---------- Services ----------
#     SVC_SYS_INFO: str = "0000120a-0000-1000-8000-00805f9b34fb"  # IP / FW
#     SVC_WIFI_CFG: str = "000012aa-0000-1000-8000-00805f9b34fb"  # WIFI/MQTT/MODE/CMD
#     SVC_MODEL: str = "000012c0-0000-1000-8000-00805f9b34fb"     # Model

#     # ---------- Characteristics (120A) ----------
#     CH_IP: str = "0000121a-0000-1000-8000-00805f9b34fb"         # IP (read)
#     CH_FW: str = "00002a26-0000-1000-8000-00805f9b34fb"         # FW version (read)

#     # ---------- Characteristics (12AA) ----------
#     CH_WIFI_COMBO: str = "000012a1-0000-1000-8000-00805f9b34fb" # ssid + 0x00 + password (write)
#     CH_MODE: str = "000012a5-0000-1000-8000-00805f9b34fb"       # "0" AWS / "1" LOCAL (write text)
#     CH_MQTT: str = "000012a6-0000-1000-8000-00805f9b34fb"       # mqtt string (write text)
#     CH_COMMAND: str = "000012a4-0000-1000-8000-00805f9b34fb"    # "reset" (write text)

#     # ---------- Characteristics (12C0) ----------
#     CH_MODEL: str = "00000000-0000-1000-8000-00805f9b34fb"      # model string (read)

#     # ---------- Builders ----------
#     @staticmethod
#     def build_mode_text(mode: str) -> str:
#         """
#         裝置規格：寫入「文字」
#         - "0" = AWS
#         - "1" = LOCAL
#         """
#         m = (mode or "").strip().upper()
#         return "0" if m == "AWS" else "1"

#     @staticmethod
#     def build_wifi_combo(ssid: str, password: str) -> bytes:
#         """
#         裝置規格：12A1 一次寫入
#         payload = ssid(utf-8) + 0x00 + password(utf-8)
#         """
#         s = (ssid or "").encode("utf-8")
#         p = (password or "").encode("utf-8")
#         return s + b"\x00" + p

#     @staticmethod
#     def build_mqtt_text(profile_mqtt: str) -> str:
#         """
#         你現在 UI 的 mqtt 欄位只填 broker ip，例如：10.10.10.10
#         寫入要變成：10.10.10.10:1883/test/test

#         若使用者已填完整（包含 '/'），就直接尊重使用者。
#         """
#         s = (profile_mqtt or "").strip()
#         if not s:
#             return ""
#         if "/" in s:
#             return s
#         if ":" not in s:
#             s = f"{s}:1883"
#         return f"{s}/test/test"

#     def as_dict(self) -> Dict[str, str]:
#         """
#         給 debug 用：把 UUID 集中吐出來
#         """
#         return {
#             "SVC_SYS_INFO": self.SVC_SYS_INFO,
#             "SVC_WIFI_CFG": self.SVC_WIFI_CFG,
#             "SVC_MODEL": self.SVC_MODEL,
#             "CH_IP": self.CH_IP,
#             "CH_FW": self.CH_FW,
#             "CH_WIFI_COMBO": self.CH_WIFI_COMBO,
#             "CH_MODE": self.CH_MODE,
#             "CH_MQTT": self.CH_MQTT,
#             "CH_COMMAND": self.CH_COMMAND,
#             "CH_MODEL": self.CH_MODEL,
#         }


# PROFILE = Zp2GattProfile()
# profiles/zp2_profile.py
from dataclasses import dataclass
from typing import NamedTuple


class GattEndpoint(NamedTuple):
    service: str
    char: str


@dataclass(frozen=True)
class Zp2Profile:
    """
    ZP2 Portable Profile (endpoint + explicit encode/decode)

    原則：
    - 不做 I/O（不依賴 Bleak / asyncio）
    - run.py 發布動作：read+ip / write+mode / write+mqtt / write+wifi...
    - profile 只負責：
      1) 正確的 endpoint（service + char）
      2) 規格一致的 encode/decode（「文字就回 str」，binary layout 才回 bytes）
    """

    key: str = "ZP2"

    # ============================================================
    # Endpoints（語意 = endpoint，只定義一次）
    # ============================================================
    EP_IP           = GattEndpoint( service="0000120a-0000-1000-8000-00805f9b34fb", char="0000121a-0000-1000-8000-00805f9b34fb",)

    EP_FW_VERSION   = GattEndpoint( service="0000120a-0000-1000-8000-00805f9b34fb", char="00002a26-0000-1000-8000-00805f9b34fb",)

    EP_MODEL        = GattEndpoint( service="000012c0-0000-1000-8000-00805f9b34fb", char="00000000-0000-1000-8000-00805f9b34fb",)

    EP_MODE         = GattEndpoint( service="000012aa-0000-1000-8000-00805f9b34fb", char="000012a5-0000-1000-8000-00805f9b34fb",)

    EP_MQTT         = GattEndpoint( service="000012aa-0000-1000-8000-00805f9b34fb", char="000012a6-0000-1000-8000-00805f9b34fb",)

    EP_WIFI_COMBO   = GattEndpoint( service="000012aa-0000-1000-8000-00805f9b34fb", char="000012a1-0000-1000-8000-00805f9b34fb",)

    EP_COMMAND      = GattEndpoint( service="000012aa-0000-1000-8000-00805f9b34fb", char="000012a4-0000-1000-8000-00805f9b34fb",)

    # ============================================================
    # Decode helpers（bytes -> semantic）
    # ============================================================
    @staticmethod
    def decode_text(raw: bytes) -> str:
        """
        通用文字解碼：utf-8 + 去 NUL + strip
        """
        if not raw:
            return ""
        return raw.decode("utf-8", errors="replace").replace("\x00", "").strip()

    @classmethod
    def decode_ip(cls, raw: bytes) -> str:
        return cls.decode_text(raw)

    @classmethod
    def decode_fw_version(cls, raw: bytes) -> str:
        return cls.decode_text(raw)

    @classmethod
    def decode_model(cls, raw: bytes) -> str:
        return cls.decode_text(raw)

    @classmethod
    def decode_mqtt(cls, raw: bytes) -> str:
        return cls.decode_text(raw)

    @staticmethod
    def decode_mode(raw: bytes) -> str:
        """
        ZP2 規格：
        - b"0" => AWS
        - b"1" => LOCAL
        """
        b0 = (raw or b"")[:1]
        if b0 == b"0":
            return "AWS"
        if b0 == b"1":
            return "LOCAL"

        # 保底：未知值當文字回去（debug 用）
        try:
            return (raw or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    # ============================================================
    # Encode helpers（semantic -> device spec payload）
    # 注意：規格是「文字」就回 str；binary layout 才回 bytes
    # ============================================================
    @staticmethod
    def encode_mode(mode: str) -> str:
        """
        ZP2 規格：寫入「文字」
        - "0" = AWS
        - "1" = LOCAL
        """
        m = (mode or "").strip().upper()
        return "0" if m == "AWS" else "1"

    @staticmethod
    def encode_mqtt(profile_mqtt: str) -> str:
        """
        UI 欄位通常只填 broker ip，例如：10.10.10.10
        寫入要變成：10.10.10.10:1883/test/test

        若使用者已填完整（包含 '/'），就直接尊重使用者。

        回傳「文字」；由 run.py 決定如何轉成 bytes。
        """
        s = (profile_mqtt or "").strip()
        if not s:
            return ""

        if "/" in s:
            return s

        if ":" not in s:
            s = f"{s}:1883"

        return f"{s}/test/test"

    @staticmethod
    def encode_wifi_combo(ssid: str, password: str) -> bytes:
        """
        ZP2 規格：binary layout
        payload = ssid(utf-8) + 0x00 + password(utf-8)

        這不是「文字」，所以回 bytes 是合理的。
        """
        return (ssid or "").encode("utf-8") + b"\x00" + (password or "").encode("utf-8")

    @staticmethod
    def encode_command(cmd: str) -> str:
        """
        ZP2 command：寫入「文字」
        目前已知：reset 有效 / reboot 無效（但仍保留語意）
        """
        return (cmd or "").strip()
