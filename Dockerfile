# Keep the browser, CUDA/Vulkan userspace, and Python base stable across CI runs.
FROM ghcr.io/cerbsim/ngapp-base@sha256:42573eed6bdcbcedfa38b924764ef8dbd9b2963af0c043e31b4198806ea7143c
WORKDIR /app
COPY . .
RUN apt-get update && apt-get install -y --no-install-recommends libquadmath0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --break-system-packages -r requirements-ci.txt
RUN pip install --no-cache-dir --break-system-packages --no-deps .
CMD ["pytest", "tests/", "-vv", "-s"]
