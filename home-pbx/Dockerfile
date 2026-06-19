ARG BUILD_FROM
FROM ${BUILD_FROM}

# Asterisk 21 (PJSIP stack) + the Python toolchain for the Ingress web UI.
# Alpine's asterisk package ships res_pjsip and the chan_pjsip channel driver,
# which is what we use throughout (chan_sip is dead in modern Asterisk).
RUN apk add --no-cache \
        asterisk \
        asterisk-sample-config \
        asterisk-pjsip \
        asterisk-sounds-en \
        python3 \
        py3-pip \
    && pip install --no-cache-dir --break-system-packages \
        "fastapi==0.115.*" \
        "uvicorn==0.32.*" \
        "jinja2==3.1.*" \
    && rm -rf /var/cache/apk/*

# Add-on rootfs: s6 service definitions, the config generator, and the web UI.
COPY rootfs /

RUN chmod +x /usr/bin/pbx-generate-config \
    && chmod +x /etc/s6-overlay/s6-rc.d/init-pbx/up \
    && chmod +x /etc/s6-overlay/s6-rc.d/asterisk/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/webui/run

# Asterisk runtime + RTP/SIP. Host networking is set in config.yaml; these are
# documentation of the surface only.
EXPOSE 5060/udp
EXPOSE 8099/tcp

LABEL \
    io.hass.name="Home PBX" \
    io.hass.description="Asterisk PBX for analog home phones" \
    io.hass.type="addon" \
    io.hass.version="0.1.0"
