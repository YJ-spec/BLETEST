# FROM python:3.11-alpine

# # 安裝 paho-mqtt 套件
# RUN pip install --no-cache-dir paho-mqtt requests flask PyYAML bleak

# # 拷貝檔案
# COPY config.yaml /config.yaml
# COPY run.py /run.py

# # 單一入口：同時啟兩支程式

# CMD ["python3", "/run.py"]
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    dbus \
    bluez \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir bleak fastapi uvicorn

COPY run.py /run.py
COPY profil_store.py /profil_store.py
COPY web /web

EXPOSE 8080

CMD ["python", "/run.py"]
