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
    && cmake --build /src/build -j"$(nproc)" --target whisper-cli \
    && install -D /src/build/bin/whisper-cli /out/usr/bin/whisper-cli \
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
    && pip install --no-cache-dir --break-system-packages \
        "fastapi==0.115.*" \
        "uvicorn==0.32.*" \
        "jinja2==3.1.*" \
    && rm -rf /var/cache/apk/*

# Optional extras, best-effort: music-on-hold sounds and the Opus codec. Package
# availability varies by Alpine release, so a miss must not fail the build.
RUN apk add --no-cache asterisk-sounds-moh 2>/dev/null || true; \
    apk add --no-cache asterisk-opus 2>/dev/null \
      || apk add --no-cache asterisk-codec-opus 2>/dev/null \
      || echo "NOTE: no prebuilt Opus codec package; Opus disabled at runtime"

# whisper-cli + the recognition model, from the builder stage.
COPY --from=whisper-builder /out/ /

# Add-on rootfs: s6 service definitions, the config generator, the web UI, and
# the voice-operator scripts + prompt audio.
COPY rootfs /

RUN chmod +x /usr/bin/switchboard-config \
    && chmod +x /usr/bin/switchboard-stt \
    && chmod +x /var/lib/asterisk/agi-bin/switchboard-operator.agi \
    && chmod +x /etc/s6-overlay/s6-rc.d/init-switchboard/up \
    && chmod +x /etc/s6-overlay/s6-rc.d/asterisk/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/webui/run

EXPOSE 5060/udp
EXPOSE 8099/tcp

LABEL \
    io.hass.name="Switchboard" \
    io.hass.description="Asterisk PBX for analog home phones" \
    io.hass.type="addon" \
    io.hass.version="0.2.7"
