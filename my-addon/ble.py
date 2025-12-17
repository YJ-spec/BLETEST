import asyncio
from bleak import BleakScanner

async def main():
    devices = await BleakScanner.discover(timeout=8)
    for d in devices:
        print(d.address, d.name, d.metadata)

asyncio.run(main())