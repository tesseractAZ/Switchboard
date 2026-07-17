ARG BUILD_FROM

# --------------------------------------------------------------------------- #
# Stage 1 — build whisper.cpp (offline speech-to-text for the voice operator).
# Vosk ships only glibc (manylinux) wheels and no Alpine apk, so it can't run on
# musl; whisper.cpp is plain C++/CMake and builds cleanly against musl. This
# builder stage compiles a static whisper-cli and fetches the model; the
# multi-stage COPY keeps the toolchain + sources out of the shipped image.
# --------------------------------------------------------------------------- #
FROM ${BUILD_FROM} AS whisper-builder
ARG WHISPER_REF=v1.7.4
ARG WHISPER_MODEL=base.en
RUN apk add --no-cache build-base cmake git wget
RUN git clone --depth 1 --branch "${WHISPER_REF}" \
        https://github.com/ggerganov/whisper.cpp /src \
    && cmake -S /src -B /src/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_OPENMP=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_EXAMPLES=ON \
    && cmake --build /src/build -j"$(nproc)" --target whisper-cli whisper-server \
    && install -D /src/build/bin/whisper-cli   /out/usr/bin/whisper-cli \
    && install -D /src/build/bin/whisper-server /out/usr/bin/whisper-server \
    && mkdir -p /out/usr/share/whisper \
    && wget -qO /out/usr/share/whisper/model.bin \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_MODEL}.bin"

# --------------------------------------------------------------------------- #
# Stage 2 — the add-on image.
# Asterisk 21 (PJSIP ships inside the main `asterisk` package on Alpine; chan_sip
# is dead). asterisk-sample-config provides modules.conf. libstdc++ is the only
# extra runtime dep whisper-cli needs (built static, OpenMP off).
# --------------------------------------------------------------------------- #
FROM ${BUILD_FROM}

RUN apk add --no-cache \
        asterisk \
        asterisk-sample-config \
        python3 \
        py3-pip \
        libstdc++ \
        tzdata \
    && pip install --no-cache-dir --break-system-packages \
        "fastapi==0.115.*" \
        "uvicorn==0.32.*" \
        "jinja2==3.1.*" \
    && rm -rf /var/cache/apk/*

# Optional extra, best-effort: music-on-hold sounds. (No Opus/HD codec package —
# Switchboard is G.711 µ-law only, so there is nothing to negotiate up to.)
RUN apk add --no-cache asterisk-sounds-moh 2>/dev/null || true

# espeak-ng: tiny offline text-to-speech, used by the home-automation voice
# operator to SPEAK dynamic strings (light names, on/off state, the room/light
# lists) that have no canned prompt and that Asterisk's SayUnixTime/SayNumber
# can't produce. Best-effort — if it's unavailable the automation flow degrades
# to canned prompts only (see switchboard-tts, which no-ops gracefully).
RUN apk add --no-cache espeak-ng 2>/dev/null \
      || echo "NOTE: espeak-ng unavailable; home-automation voice readback limited to canned prompts"

# ffmpeg: transcode a fetched TTS clip to the 8 kHz mono WAV Asterisk Playback needs.
# Home Assistant's tts_proxy serves MP3 by default (even for Piper), which the pure-
# Python WAV reader can't decode — ffmpeg handles MP3/OGG/etc. Best-effort: without it
# the announce {url} branch is limited to WAV input (the {text} branch still works).
RUN apk add --no-cache ffmpeg 2>/dev/null \
      || echo "NOTE: ffmpeg unavailable; announce url-branch limited to WAV input"

# Asterisk modules for the operator page-all intercom (ConfBridge / Page) and
# message-waiting stutter tone (res_mwi_external). PJSIP, app_confbridge,
# app_page and res_mwi_external all ship inside the main `asterisk` package on
# Alpine 21; these explicit best-effort pulls are belt-and-suspenders for any
# release that splits them out, and never fail the build.
RUN for p in asterisk-page asterisk-confbridge asterisk-mwi-external; do \
        apk add --no-cache "$p" 2>/dev/null || true; \
    done; true

# Asterisk core English sound files (digits + time words) — needed by
# SayUnixTime / SayNumber for the talking clock and wake-up calls. µ-law matches
# the analog FXS path. Best-effort: a download hiccup must not fail the whole
# image (the clock just stays silent until the files are present).
ARG CORE_SOUNDS=asterisk-core-sounds-en-ulaw-current.tar.gz
RUN mkdir -p /var/lib/asterisk/sounds/en \
    && ( wget -qO /tmp/cs.tgz "https://downloads.asterisk.org/pub/telephony/sounds/${CORE_SOUNDS}" \
         && tar -xzf /tmp/cs.tgz -C /var/lib/asterisk/sounds/en \
         && rm -f /tmp/cs.tgz \
         && echo "core sounds installed ($(ls /var/lib/asterisk/sounds/en/digits | wc -l) digit files)" ) \
       || echo "NOTE: core sound download failed; time speech unavailable until added"

# whisper-cli + the recognition model, from the builder stage.
COPY --from=whisper-builder /out/ /

# Add-on rootfs: s6 service definitions, the config generator, the web UI, and
# the voice-operator scripts + prompt audio.
COPY rootfs /

RUN chmod +x /usr/bin/switchboard-config \
    && chmod +x /usr/bin/switchboard-stt \
    && chmod +x /usr/bin/switchboard-tts \
    && chmod +x /usr/bin/switchboard-mwi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-operator.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-wakeup.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-automation.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-clock.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-directory.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-status.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-announce.agi \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-wakeup-deliver.agi \
    && chmod +x /usr/share/switchboard/console/console.py \
    && chmod +x /usr/share/switchboard/console-web/server.py \
    && chmod +x /usr/share/switchboard/wakeup/scheduler.py \
    && chmod +x /etc/s6-overlay/s6-rc.d/init-switchboard/up \
    && chmod +x /etc/s6-overlay/s6-rc.d/asterisk/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/whisper-server/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/webui/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/operator-console/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/console-web/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/wakeup-scheduler/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/devhealth/run \
    && chmod +x /usr/share/switchboard/devhealth/poller.py

EXPOSE 5060/udp
EXPOSE 8099/tcp
EXPOSE 8100/tcp

LABEL \
    io.hass.name="Switchboard" \
    io.hass.description="Asterisk PBX for analog home phones" \
    io.hass.type="addon" \
    io.hass.version="0.34.0"
