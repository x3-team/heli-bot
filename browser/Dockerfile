# One Chromium container per user. The pool applies --memory=1g --cpus=1.
# noVNC is for the user to watch or step in. The container is not privileged.
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      novnc \
      websockify \
      x11vnc \
      xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1001 heli \
    && mkdir -p /profile \
    && chown heli:heli /profile

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

USER heli
ENV DISPLAY=:99
ENV PROFILE_DIR=/profile
EXPOSE 6080
ENTRYPOINT ["/entrypoint.sh"]
