# Experimental only. Never part of the CLI, installer or product dependency set.
FROM flowly-screen-gateway-test:bookworm
USER root
RUN uv pip install --python /opt/flowly/.venv/bin/python aiortc==1.14.0 av==16.1.0 mss==10.1.0
USER screen-test
USER root
COPY xvnc-benchmark-wrapper.py /usr/local/lib/flowly-benchmark-xvnc
RUN chmod 755 /usr/local/lib/flowly-benchmark-xvnc && rm /usr/bin/Xvnc && ln -s /usr/local/lib/flowly-benchmark-xvnc /usr/bin/Xvnc
USER screen-test
