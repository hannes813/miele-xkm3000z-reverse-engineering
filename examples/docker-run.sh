#!/bin/sh
set -eu

docker run --rm -it --network host \
  -v /volume2/docker/zigbee2mqtt:/work \
  -e MIELE_ZNP_HOST=192.168.xxx.xxx \
  -e MIELE_ZNP_PORT=6638 \
  -e MIELE_TARGET_NWK=0x537D \
  -e MQTT_HOST=192.168.xxx.xx \
  -e MQTT_USER='your User' \
  -e MQTT_PASS='your-password' \
  -e MQTT_BASE=miele_xkm3000z \
  python:3.12-alpine \
  sh -c "apk add --no-cache mosquitto-clients && python /work/src/miele_gateway_mqtt.py"
