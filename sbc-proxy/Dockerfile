# Kamailio SBC Proxy for Teams → LiveKit
FROM debian:bookworm-slim

# Install Kamailio and dependencies
RUN apt-get update && apt-get install -y \
    gnupg \
    wget \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Add Kamailio repository
RUN wget -O- https://deb.kamailio.org/kamailiodebkey.gpg | gpg --dearmor > /usr/share/keyrings/kamailio.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/kamailio.gpg] https://deb.kamailio.org/kamailio58 bookworm main" > /etc/apt/sources.list.d/kamailio.list

# Install Kamailio with TLS support and extra modules (uac, rtimer)
RUN apt-get update && apt-get install -y \
    kamailio \
    kamailio-tls-modules \
    kamailio-utils-modules \
    kamailio-extra-modules \
    netbase \
    && rm -rf /var/lib/apt/lists/*

# Create directories
RUN mkdir -p /etc/kamailio/tls

# Copy configuration
COPY kamailio.cfg /etc/kamailio/kamailio.cfg

# Expose SIP ports
# 5060 - SIP UDP/TCP (for LiveKit)
# 5061 - SIP TLS (for Teams)
EXPOSE 5060/udp 5060/tcp 5061/tcp

# Run Kamailio in foreground
CMD ["kamailio", "-DD", "-E", "-m", "64", "-M", "8"]
