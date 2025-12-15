# Teams → LiveKit SBC Proxy

A lightweight SIP proxy using Kamailio to bridge Microsoft Teams Direct Routing to LiveKit SIP.

## Architecture

```
Teams User → Teams Direct Routing → This SBC → LiveKit SIP → LiveKit Room → AI Agent
                                      ↑
                            sbc.yourdomain.com
                           (verified in M365)
```

The SBC only handles SIP signaling (~5-10 messages per call). Audio flows directly between Teams and LiveKit.

## Prerequisites

- A domain you control (e.g., `yourdomain.com`)
- GCP account with billing enabled
- Microsoft 365 with Teams Phone license
- LiveKit Cloud account with SIP enabled

## Quick Start

### 1. Configure Variables

```bash
cp env.example .env
# Edit .env with your values
```

### 2. Deploy to GCP

```bash
# Make the deploy script executable
chmod +x deploy.sh

# Deploy (creates VM, configures firewall, installs Kamailio)
./deploy.sh
```

### 3. Set Up DNS

Point your SBC domain to the VM's external IP:
```
sbc.yourdomain.com → [VM_EXTERNAL_IP]
```

### 4. Get TLS Certificate

SSH into the VM and run:
```bash
sudo certbot certonly --standalone -d sbc.yourdomain.com
```

### 5. Configure Teams Direct Routing

See [TEAMS_LIVEKIT_SETUP_GUIDE.md](../docs/TEAMS_LIVEKIT_SETUP_GUIDE.md) for Teams configuration.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds Kamailio container |
| `kamailio.cfg` | Kamailio SIP proxy configuration |
| `tls.cfg` | TLS configuration for secure SIP |
| `docker-compose.yml` | Local testing |
| `deploy.sh` | GCP deployment script |
| `setup-tls.sh` | Helper to configure TLS after certbot |
| `env.example` | Environment variables template |

## Configuration

### Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SBC_DOMAIN` | Your SBC domain (verified in M365) | `sbc.yourdomain.com` |
| `LIVEKIT_SIP_URI` | LiveKit SIP endpoint | `your-project.sip.livekit.cloud` |
| `LIVEKIT_SIP_PORT` | LiveKit SIP port | `5060` |
| `GCP_PROJECT` | GCP project ID | `my-project` |
| `GCP_ZONE` | GCP zone for VM | `us-central1-a` |

## Local Testing

```bash
# Start Kamailio locally
docker-compose up -d

# Check logs
docker-compose logs -f

# Test SIP connectivity
docker exec kamailio kamcmd ul.dump
```

## Monitoring

SSH into the VM:
```bash
# Check Kamailio status
sudo systemctl status kamailio

# View logs
sudo journalctl -u kamailio -f

# Check active calls
kamcmd ul.dump

# Check statistics
kamcmd stats.get_statistics all
```

## Troubleshooting

### SBC shows "Offline" in Teams Admin Center

1. Check DNS resolution: `nslookup sbc.yourdomain.com`
2. Check TLS certificate: `openssl s_client -connect sbc.yourdomain.com:5061`
3. Check firewall: `sudo ufw status` or GCP firewall rules
4. Check Kamailio is running: `sudo systemctl status kamailio`

### Calls fail with "Cannot route"

1. Check Kamailio logs: `sudo journalctl -u kamailio -f`
2. Verify LiveKit SIP URI is correct in `kamailio.cfg`
3. Test connectivity to LiveKit: `nc -zv your-project.sip.livekit.cloud 5060`

### No audio

1. Verify both Teams and LiveKit have public IPs
2. Check if media relay is needed (rare)
3. Verify RTP ports are open (UDP 10000-60000)

## Cost

| Component | Monthly Cost |
|-----------|--------------|
| GCP e2-micro VM | ~$6-8 |
| Static IP | ~$3 |
| Disk (10GB) | ~$1 |
| Network egress | ~$1-5 |
| **Total** | **~$12-17** |

## Capacity

With signaling-only mode (default):
- **1,000+ concurrent calls** on e2-micro
- SIP messages are ~1KB each
- Audio bypasses the SBC entirely

## Security

- TLS required for Teams (port 5061)
- Restrict inbound IPs to Microsoft ranges if desired
- No audio data passes through the SBC
