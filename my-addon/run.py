import asyncio
from bleak import BleakScanner

def safe_get(obj, key, default=None):
    return getattr(obj, key, default)

async def main():
    devices = await BleakScanner.discover(timeout=8.0)

    for d in devices:
        address = safe_get(d, "address")
        name = safe_get(d, "name")
        rssi = safe_get(d, "rssi")

        # bleak 不同版本可能叫 details / _details，Linux 通常會塞 BlueZ 的資訊在這
        details = safe_get(d, "details", None)
        _details = safe_get(d, "_details", None)

        print("----")
        print(f"address: {address}")
        print(f"name:    {name}")
        print(f"rssi:    {rssi}")
        if details is not None:
            print(f"details: {details}")
        if _details is not None:
            print(f"_details:{_details}")

        # 也順便印出有哪些欄位，方便你一次看清楚
        # （看完可刪）
        print("attrs:", [a for a in dir(d) if not a.startswith("__")])

asyncio.run(main())
