#!/bin/bash
#
# Set up TLS certificates for Kamailio after running certbot
#
# Usage: ./setup-tls.sh sbc.yourdomain.com
#

set -e

DOMAIN=${1:-$SBC_DOMAIN}

if [ -z "$DOMAIN" ]; then
    echo "Usage: ./setup-tls.sh <domain>"
    echo "Example: ./setup-tls.sh sbc.yourdomain.com"
    exit 1
fi

echo "Setting up TLS for $DOMAIN"

# Create TLS directory
mkdir -p ./tls

# Check if certbot certificates exist
CERT_PATH="/etc/letsencrypt/live/$DOMAIN"

if [ ! -d "$CERT_PATH" ]; then
    echo "Certificates not found at $CERT_PATH"
    echo "Run certbot first:"
    echo "  sudo certbot certonly --standalone -d $DOMAIN"
    exit 1
fi

# Copy certificates (need sudo)
echo "Copying certificates..."
sudo cp "$CERT_PATH/fullchain.pem" ./tls/server.pem
sudo cp "$CERT_PATH/privkey.pem" ./tls/server.key
sudo cp "$CERT_PATH/chain.pem" ./tls/ca.pem

# Fix permissions
sudo chown $(whoami):$(whoami) ./tls/*
chmod 600 ./tls/server.key
chmod 644 ./tls/server.pem ./tls/ca.pem

echo "TLS certificates configured in ./tls/"
echo ""
echo "Now update kamailio.cfg:"
echo "  1. Replace 'sbc.yourdomain.com' with '$DOMAIN'"
echo "  2. Replace 'your-project.sip.livekit.cloud' with your LiveKit SIP URI"
echo ""
echo "Then start the SBC:"
echo "  docker compose up -d"
