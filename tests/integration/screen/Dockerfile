# Disposable Linux screen integration runner, not the product distribution.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server xauth openbox dbus-x11 chromium chromium-sandbox \
    xdotool x11-utils fonts-dejavu-core procps \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir filelock==3.19.1 pytest==9.0.2 \
    pytest-asyncio==1.3.0 aiohttp==3.13.3 playwright==1.58.0
RUN useradd --create-home --uid 1000 screen-test \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
USER screen-test
WORKDIR /workspace
ENV PYTHONPATH=/workspace
CMD ["python", "-m", "pytest", "tests/test_screen_authority.py", "tests/test_screen_rfb.py", "-q"]
