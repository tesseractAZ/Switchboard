ARG BUILD_FROM
FROM ${BUILD_FROM}

# Asterisk 21 + the Python toolchain for the Ingress web UI.
# On Alpine the res_pjsip / chan_pjsip stack ships INSIDE the main `asterisk`
# package (there is no separate asterisk-pjsip package), which is what we use
# throughout (chan_sip is dead in modern Asterisk). asterisk-sample-config
# provides modules.conf (autoload=yes) so the pjsip modules load.
RUN apk add --no-cache \
        asterisk \
        asterisk-sample-config \
        python3 \
        py3-pip \
    && pip install --no-cache-dir --break-system-packages \
        "fastapi==0.115.*" \
        "uvicorn==0.32.*" \
        "jinja2==3.1.*" \
    && rm -rf /var/cache/apk/*

# Optional extras, best-effort: music-on-hold sounds and the Opus codec. Package
# availability varies by Alpine release, so a miss must not fail the build.
# Without Opus, G.711/G.722 still work and "allow=opus" is simply not offered.
RUN apk add --no-cache asterisk-sounds-moh 2>/dev/null || true; \
    apk add --no-cache asterisk-opus 2>/dev/null \
      || apk add --no-cache asterisk-codec-opus 2>/dev/null \
      || echo "NOTE: no prebuilt Opus codec package; Opus disabled at runtime"

# Add-on rootfs: s6 service definitions, the config generator, and the web UI.
COPY rootfs /

RUN chmod +x /usr/bin/switchboard-config \
    && chmod +x /etc/s6-overlay/s6-rc.d/init-switchboard/up \
    && chmod +x /etc/s6-overlay/s6-rc.d/asterisk/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/webui/run

EXPOSE 5060/udp
EXPOSE 8099/tcp

LABEL \
    io.hass.name="Switchboard" \
    io.hass.description="Asterisk PBX for analog home phones" \
    io.hass.type="addon" \
    io.hass.version="0.1.3"
