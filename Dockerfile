FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY toshiba2mqtt.py .

# Config is provided at runtime via a mounted volume or environment variables.
# Mount your config at /app/config.yaml, or set TOSHIBA_* / MQTT_* env vars.
ENV TOSHIBA2MQTT_CONFIG=/app/config.yaml

ENTRYPOINT ["python", "toshiba2mqtt.py"]
