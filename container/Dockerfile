FROM astronrd/linc:latest

ARG LWA_SOLAR_UTIL_REPO=https://github.com/ovro-eovsa/lwa-solar-util.git
ARG LWA_SOLAR_UTIL_REF=main

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    MPLCONFIGDIR=/tmp/matplotlib \
    SUNPY_CONFIGDIR=/tmp/sunpy \
    PATH=/opt/dp3-6.5.1/bin:${PATH} \
    PYTHONPATH=/opt/dp3-6.5.1/lib/python3.12/site-packages:${PYTHONPATH}

USER root

RUN if command -v apt-get >/dev/null 2>&1; then \
        apt-get update && \
        apt-get install -y --no-install-recommends ca-certificates git python3-pip && \
        rm -rf /var/lib/apt/lists/*; \
    fi

RUN python3 -m pip install --break-system-packages --no-build-isolation "git+${LWA_SOLAR_UTIL_REPO}@${LWA_SOLAR_UTIL_REF}"

WORKDIR /opt/lwasolarproc
COPY . /opt/lwasolarproc

RUN python3 -m pip install --break-system-packages --no-build-isolation .

ENTRYPOINT ["/bin/bash", "-lc"]
CMD ["lwasolarproc-fullband --help"]
