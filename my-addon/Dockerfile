FROM python:3.11-alpine

# 安裝 paho-mqtt 套件
RUN pip install --no-cache-dir paho-mqtt requests flask PyYAML

# 拷貝檔案
COPY config.yaml /config.yaml
COPY run.py /run.py

# 單一入口：同時啟兩支程式

CMD ["python3", "/run.py"]
