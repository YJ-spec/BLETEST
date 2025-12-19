FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    dbus \
    bluez \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir bleak fastapi uvicorn

COPY run.py /run.py
COPY profile_store.py /profile_store.py
COPY gatt_profiles.py /gatt_profiles.py
COPY web /web
COPY profiles /profiles

EXPOSE 8080

CMD ["python", "/run.py"]
