FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    dbus \
    bluez \
    && rm -rf /var/lib/apt/lists/*
# 安裝 paho-mqtt 套件
RUN pip install --no-cache-dir bleak fastapi uvicorn

# 拷貝檔案
COPY run.py /run.py
COPY web ./web

# 單一入口：同時啟兩支程式

CMD ["python3", "/run.py"]
