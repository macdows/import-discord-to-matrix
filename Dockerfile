# --- Builder stage ---
FROM python:3.12-alpine AS builder

RUN apk add --no-cache \
    cmake gcc g++ make git libffi-dev python3-dev

# Wrap cmake to inject the cmake 4.x compatibility flag.
# python-olm's build script hard-codes its own cmake call without it.
RUN mv /usr/bin/cmake /usr/bin/cmake.real \
    && printf '#!/bin/sh\ncase "$1" in\n  --build|--install|-E) exec cmake.real "$@" ;;\n  *) exec cmake.real -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "$@" ;;\nesac\n' > /usr/bin/cmake \
    && chmod +x /usr/bin/cmake

# Build libolm 3.2.16 from source
RUN git clone --branch 3.2.16 --depth 1 https://gitlab.matrix.org/matrix-org/olm.git /tmp/olm \
    && cd /tmp/olm \
    && cmake -B build -DCMAKE_INSTALL_PREFIX=/usr \
    && cmake --build build --parallel \
    && cmake --install build \
    && rm -rf /tmp/olm

RUN pip install --no-cache-dir "matrix-nio[e2e]" requests Pillow

# --- Runtime stage ---
FROM python:3.12-alpine

# libolm shared libraries
COPY --from=builder /usr/lib/libolm* /usr/lib/

# Installed Python packages
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# Runtime C++ standard library (needed by libolm)
RUN apk add --no-cache libstdc++

COPY import_discord_to_matrix.py /app/import_discord_to_matrix.py

WORKDIR /data

ENTRYPOINT ["python", "/app/import_discord_to_matrix.py"]
