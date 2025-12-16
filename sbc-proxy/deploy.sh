#!/bin/bash
#
# Deploy Teams SBC Proxy to GCP Compute Engine
#
# Prerequisites:
# - gcloud CLI installed and authenticated
# - .env file configured
#

set -e

# Load environment variables
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "Error: .env file not found. Copy env.example to .env and configure."
    exit 1
fi

# Validate required variables
: "${GCP_PROJECT:?GCP_PROJECT is required}"
: "${GCP_ZONE:?GCP_ZONE is required}"
: "${GCP_VM_NAME:?GCP_VM_NAME is required}"
: "${SBC_DOMAIN:?SBC_DOMAIN is required}"
: "${LIVEKIT_SIP_URI:?LIVEKIT_SIP_URI is required}"

echo "=== Teams SBC Proxy Deployment ==="
echo "Project: $GCP_PROJECT"
echo "Zone: $GCP_ZONE"
echo "VM Name: $GCP_VM_NAME"
echo "SBC Domain: $SBC_DOMAIN"
echo "LiveKit SIP: $LIVEKIT_SIP_URI"
echo ""

# Set project
gcloud config set project $GCP_PROJECT

# Create firewall rules for SIP
echo "=== Creating firewall rules ==="
gcloud compute firewall-rules create allow-sip-tls \
    --allow=tcp:5061 \
    --target-tags=sbc-proxy \
    --description="Allow SIP TLS from Teams" \
    2>/dev/null || echo "Firewall rule 'allow-sip-tls' already exists"

gcloud compute firewall-rules create allow-sip-udp \
    --allow=udp:5060 \
    --target-tags=sbc-proxy \
    --description="Allow SIP UDP" \
    2>/dev/null || echo "Firewall rule 'allow-sip-udp' already exists"

gcloud compute firewall-rules create allow-sip-tcp \
    --allow=tcp:5060 \
    --target-tags=sbc-proxy \
    --description="Allow SIP TCP" \
    2>/dev/null || echo "Firewall rule 'allow-sip-tcp' already exists"

gcloud compute firewall-rules create allow-http-certbot \
    --allow=tcp:80 \
    --target-tags=sbc-proxy \
    --description="Allow HTTP for Certbot" \
    2>/dev/null || echo "Firewall rule 'allow-http-certbot' already exists"

# Create static IP
echo "=== Creating static IP ==="
gcloud compute addresses create $GCP_VM_NAME-ip \
    --region=$(echo $GCP_ZONE | sed 's/-[a-z]$//') \
    2>/dev/null || echo "Static IP '$GCP_VM_NAME-ip' already exists"

STATIC_IP=$(gcloud compute addresses describe $GCP_VM_NAME-ip \
    --region=$(echo $GCP_ZONE | sed 's/-[a-z]$//') \
    --format='get(address)')

echo "Static IP: $STATIC_IP"

# Create startup script
cat > /tmp/startup-script.sh << 'STARTUP'
#!/bin/bash
set -e

# Install Docker
apt-get update
apt-get install -y apt-transport-https ca-certificates curl gnupg lsb-release

curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Install certbot for TLS certificates
apt-get install -y certbot

# Enable Docker
systemctl enable docker
systemctl start docker

echo "Startup complete. Run certbot to get TLS certificates."
STARTUP

# Create VM
echo "=== Creating VM ==="
gcloud compute instances create $GCP_VM_NAME \
    --zone=$GCP_ZONE \
    --machine-type=e2-micro \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=10GB \
    --boot-disk-type=pd-standard \
    --tags=sbc-proxy \
    --address=$STATIC_IP \
    --metadata-from-file=startup-script=/tmp/startup-script.sh \
    2>/dev/null || echo "VM '$GCP_VM_NAME' already exists"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. Point DNS: $SBC_DOMAIN → $STATIC_IP"
echo ""
echo "2. Wait for DNS propagation, then SSH and get TLS certificate:"
echo "   gcloud compute ssh $GCP_VM_NAME --zone=$GCP_ZONE"
echo "   sudo certbot certonly --standalone -d $SBC_DOMAIN"
echo ""
echo "3. Copy the SBC files to the VM:"
echo "   gcloud compute scp --recurse ./* $GCP_VM_NAME:~/sbc-proxy/ --zone=$GCP_ZONE"
echo ""
echo "4. Update kamailio.cfg with your domain and LiveKit URI"
echo ""
echo "5. Start the SBC:"
echo "   cd ~/sbc-proxy && sudo docker compose up -d"
echo ""
echo "6. Configure Teams Direct Routing to use: $SBC_DOMAIN"
