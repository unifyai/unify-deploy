# Microsoft Teams + LiveKit SIP Integration Setup Guide

This guide walks you through setting up LiveKit voice assistants that can receive calls from Microsoft Teams users using SIP and Direct Routing.

## Table of Contents
1. [Overview](#overview)
   - [Architecture](#architecture)
   - [How It Works](#how-it-works)
   - [Understanding Teams Calling Systems](#understanding-teams-calling-systems)
   - [Approaches to Connect Teams Users to Your SBC](#approaches-to-connect-teams-users-to-your-sbc)
   - [Understanding SIP Call Flow](#understanding-sip-call-flow)
   - [Understanding SDP and SRTP Negotiation](#understanding-sdp-and-srtp-negotiation)
   - [Deep Dive: SBC Health Checks (SIP OPTIONS & mTLS)](#deep-dive-sbc-health-checks-sip-options--mtls)
2. [Prerequisites](#prerequisites)
3. [Step 1: Choose an SBC Provider](#step-1-choose-an-sbc-provider)
4. [Step 2: Set Up Your SBC](#step-2-set-up-your-sbc)
5. [Step 3: Configure Teams Direct Routing](#step-3-configure-teams-direct-routing)
6. [Step 4: Create Resource Account](#step-4-create-resource-account)
7. [Step 5: Configure Voice Routing](#step-5-configure-voice-routing)
8. [Step 6: Configure LiveKit SIP Trunk](#step-6-configure-livekit-sip-trunk)
9. [Step 7: Update Your Application](#step-7-update-your-application)
10. [Step 8: Test the Integration](#step-8-test-the-integration)
11. [Troubleshooting](#troubleshooting)
12. [Known Issues](#known-issues)
13. [Appendix A: Self-Hosted Kamailio SBC Setup](#appendix-a-self-hosted-kamailio-sbc-setup)
14. [Appendix B: Files Reference](#appendix-b-files-reference)

---

## Overview

### Architecture

This setup mirrors your existing Twilio → LiveKit phone integration:

```
Current (Phone):
Phone Call → Twilio → SIP → LiveKit Room → AI Agent

Teams Integration:
Teams Call → Direct Routing → SBC → SIP → LiveKit Room → AI Agent
```

### How It Works

1. Teams user calls a **Resource Account** (virtual identity, no phone number needed)
2. Teams routes the call via **Direct Routing** to your **Session Border Controller (SBC)**
3. SBC translates the call to **SIP** and forwards to **LiveKit's SIP trunk**
4. LiveKit creates a room and your **AI agent** joins to handle the call

### Understanding Teams Calling Systems

Teams has **two completely separate calling systems**, and understanding this is crucial for making Direct Routing work:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     TEAMS CALLING SYSTEMS                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  SYSTEM 1: Teams-to-Teams (Internal)                               │
│  ─────────────────────────────────────                              │
│  • Call someone by name/email                                       │
│  • Stays entirely within Microsoft's cloud                          │
│  • Free, instant, no configuration needed                           │
│  • CANNOT reach your SBC (it's not a Teams user)                   │
│                                                                     │
│                                                                     │
│  SYSTEM 2: Phone System (External via Direct Routing)              │
│  ────────────────────────────────────────────────────               │
│  • Dial a phone number                                              │
│  • Uses Voice Routing → SBC → external world                        │
│  • Requires licenses, configuration                                 │
│  • CAN reach your SBC (that's what it's designed for)              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**The fundamental challenge:** Your SBC lives in "System 2" (phone world), but users naturally want to use "System 1" (call by name). The solution is to bridge these systems.

### Approaches to Connect Teams Users to Your SBC

There are three main approaches, each with different trade-offs:

#### Approach 1: Direct Phone Number Dialing

```
User opens Teams → Dial Pad → Types "+19999999999" → Call reaches SBC
```

**How it works:**
1. User dials a phone number (even a "fake" one assigned via Direct Routing)
2. Teams recognizes it as a phone number → triggers the Phone System
3. Teams checks if the **caller** has a Voice Routing Policy
4. If yes, routes the call to your SBC based on the Voice Route configuration
5. SBC receives SIP INVITE and forwards to LiveKit

**Requirements:**
- Every user who wants to call needs a Voice Routing Policy assigned
- Every user needs `EnterpriseVoiceEnabled = True`
- Users must know/remember the phone number

| Pros | Cons |
|------|------|
| Simple, direct path | Every caller needs configuration |
| Easy to debug | Users must dial a number |

---

#### Approach 2: Auto Attendant with Resource Account (Recommended)

```
User searches "AI Assistant" → Clicks Call → Auto Attendant forwards → SBC
```

**How it works:**
1. User calls the Resource Account by name (e.g., "sbc-resource")
2. This is a **Teams-to-Teams call** (System 1) - no policy needed for caller
3. The **Auto Attendant** (linked to the Resource Account) answers
4. Auto Attendant is configured to forward calls to a phone number (+19999999999)
5. The Auto Attendant (using the Resource Account's policy) makes the outbound call
6. This triggers the Phone System (System 2) with the Resource Account as caller
7. Call routes to SBC → LiveKit

**Visual flow:**
```
[User] ──Teams Call──→ [Auto Attendant] ──Phone Call──→ [SBC] ──→ [LiveKit]
         (System 1)     (has the policy)    (System 2)
         (no config)    (does forwarding)   (voice routing)
```

**Requirements:**
- Resource Account with Voice Routing Policy (configured once)
- Resource Account with a phone number (Direct Routing type)
- Auto Attendant configured to forward calls
- Regular users need **NO special configuration**

| Pros | Cons |
|------|------|
| Users call by name (natural UX) | More complex initial setup |
| No per-user configuration | Auto Attendant adds a layer |
| Scalable to entire organization | |

---

#### Approach 3: Regular M365 User Account

```
User searches "ai-assistant@company.com" → Clicks Call → ???
```

**What happens:**
1. User calls a regular M365 user by name
2. Teams recognizes it as an internal user → uses Teams-to-Teams (System 1)
3. Call goes to that user's **Teams client** (desktop/mobile app)
4. Problem: There's no AI running the Teams client - call goes nowhere useful
5. **Does NOT reach your SBC**

**Key insight:** A regular user account doesn't solve the problem because:
- Calling by name → Goes to their Teams app, not SBC
- Calling by their phone number → Same as Approach 1 (caller needs policy)

This is why **Resource Accounts + Auto Attendants exist** - they're designed specifically to bridge internal Teams calling to external destinations like your SBC.

---

### Comparison Summary

| Aspect | Approach 1: Direct Number | Approach 2: Auto Attendant | Approach 3: Regular User |
|--------|---------------------------|----------------------------|--------------------------|
| **User dials** | Phone number | Name ("AI Assistant") | Name |
| **Caller needs policy?** | ✅ Yes, every caller | ❌ No | N/A |
| **Setup complexity** | Low | Medium | Doesn't work |
| **User experience** | Must know number | Natural (search by name) | Confusing |
| **Reaches SBC?** | ✅ Yes | ✅ Yes (via forward) | ❌ No |
| **Recommended?** | For testing | ✅ For production | ❌ No |

---

### Recommended Architecture

For production use, we recommend **Approach 2 (Auto Attendant)**:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RECOMMENDED SETUP                                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Any Teams User (no special config)                                │
│       │                                                             │
│       │ Searches "AI Assistant" and clicks Call                    │
│       ▼                                                             │
│  Auto Attendant + Resource Account                                  │
│       │  • sbc-resource@yourcompany.com                            │
│       │  • Has Voice Routing Policy                                 │
│       │  • Has phone number (+19999999999, Direct Routing)         │
│       │                                                             │
│       │ Auto Attendant forwards to +19999999999                    │
│       ▼                                                             │
│  Voice Routing                                                      │
│       │  • Matches pattern ".*"                                     │
│       │  • Routes to sbc.yourcompany.com                           │
│       ▼                                                             │
│  Your SBC (Kamailio)                                                │
│       │  • Receives SIP INVITE                                      │
│       │  • Forwards to LiveKit                                      │
│       ▼                                                             │
│  LiveKit Room + AI Agent                                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Understanding SIP Call Flow

SIP (Session Initiation Protocol) separates **signaling** (call setup) from **media** (audio). This is key to understanding why an SBC can be lightweight.

#### Phase 1: Call Setup (SBC Involved)

```
1. Teams User clicks "Call AI Assistant"
   
2. Teams sends SIP INVITE to your SBC
   Teams → sbc.yourdomain.com (your SBC)
   
   INVITE sip:ai-assistant@sbc.yourdomain.com
   From: teams-user@company.com
   SDP: [Teams' IP address and ports for audio]

3. SBC receives INVITE, rewrites destination, forwards to LiveKit
   SBC → LiveKit SIP
   
   INVITE sip:room@your-project.sip.livekit.cloud

4. LiveKit accepts, sends back 200 OK with its audio details
   LiveKit → SBC → Teams

5. Teams sends ACK - call is now established
   Teams → SBC → LiveKit
```

**SBC's role:** Just forwards these SIP messages (a few KB of text). It's a "router" for call setup.

#### Phase 2: Audio Flows (SBC NOT Involved)

Once call setup completes, audio flows **directly** between Teams and LiveKit:

```
Teams ←─────── RTP Audio (UDP packets) ───────→ LiveKit

                    SBC is not in this path!
```

The SDP (Session Description Protocol) exchanged during setup contained the actual IP addresses and ports. Teams and LiveKit now talk directly.

#### Phase 3: Call Hangup (SBC Involved Again)

```
1. User hangs up → Teams sends BYE → SBC → LiveKit
2. LiveKit confirms → 200 OK → SBC → Teams
```

#### Visual Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     CALL SETUP (SIP Signaling)               │
│                                                              │
│   Teams ──INVITE──→ SBC ──INVITE──→ LiveKit                 │
│   Teams ←──200 OK── SBC ←──200 OK── LiveKit                 │
│   Teams ──ACK────→ SBC ──ACK────→ LiveKit                   │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     AUDIO (RTP Media)                        │
│                                                              │
│   Teams ←─────────── Direct UDP Audio ───────────→ LiveKit  │
│                                                              │
│                    (SBC not involved!)                       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     HANGUP (SIP Signaling)                   │
│                                                              │
│   Teams ──BYE────→ SBC ──BYE────→ LiveKit                   │
│   Teams ←──200 OK── SBC ←──200 OK── LiveKit                 │
└─────────────────────────────────────────────────────────────┘
```

#### Understanding SDP and SRTP Negotiation

The SDP (Session Description Protocol) carried in SIP messages describes **how** media should be exchanged. For Teams Direct Routing, understanding the SRTP negotiation is critical.

**Teams INVITE SDP (what Teams offers):**
```
m=audio 49170 RTP/SAVP 9 101
c=IN IP4 52.114.x.x
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:keyAAAA...
a=crypto:2 AES_CM_128_HMAC_SHA1_32 inline:keyBBBB...
```

Key elements:
- `RTP/SAVP` = Secure Audio Video Profile (encrypted)
- `crypto:1`, `crypto:2` = Two encryption options Teams offers (tags are just labels)
- `AES_CM_128_HMAC_SHA1_80` = Encryption algorithm with 80-bit authentication

**LiveKit 200 OK SDP (what LiveKit accepts):**
```
m=audio 58004 RTP/SAVP 9 101
c=IN IP4 161.115.180.7
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:keyXXXX...
```

Key elements:
- LiveKit **must** respond with `RTP/SAVP` (not `RTP/AVP`) - Teams requires encryption
- LiveKit chooses **one** of the offered crypto options (the tag indicates which)
- The `inline:` contains the SRTP master key

**Common Issues:**
| SDP Problem | Symptom | Solution |
|-------------|---------|----------|
| `RTP/AVP` instead of `RTP/SAVP` | Immediate disconnect | Enable SRTP on LiveKit trunk |
| Internal IP in `c=` line | No audio | Check LiveKit returns public IP |
| Missing `a=crypto:` | Teams rejects call | Enable SRTP on LiveKit trunk |

#### Why This Matters for Hosting

**The SBC is just a "matchmaker":**

1. Teams says "I want to call, here's my IP/port for audio"
2. SBC forwards that to LiveKit
3. LiveKit says "OK, here's MY IP/port for audio"
4. SBC forwards that back to Teams
5. **SBC steps out of the way** → Teams and LiveKit talk directly

The actual voice data (99.9% of bandwidth) never touches your SBC:
- **Signaling:** ~5-10 SIP messages per call, ~1KB each = negligible
- **Audio:** 100kbps × duration = megabytes, but goes **direct** between Teams ↔ LiveKit

| Mode | SBC Handles | Capacity on Small VM |
|------|-------------|----------------------|
| **Signaling only** | SIP messages (text, ~1KB each) | 1,000+ concurrent calls |
| **Media relay** | Audio packets (100kbps/call) | 10-50 concurrent calls |

For Teams ↔ LiveKit, you typically only need **signaling mode** since both have public IPs and can exchange audio directly. This means a tiny VM (~$6-15/month) can handle thousands of calls.

> **Note:** Media relay is only needed if firewalls block direct communication between Teams and LiveKit, which is rare since both use public cloud infrastructure.

### Deep Dive: SBC Health Checks (SIP OPTIONS & mTLS)

Before any calls can be routed, Microsoft Teams must verify your SBC is reachable and healthy. This section explains the technical protocol-level details of how this works—useful for debugging connectivity issues or understanding why an "uncertified" SBC (like Kamailio) can still work with proper configuration.

#### The Connection Establishment Problem

When you add an SBC to Teams Direct Routing, Microsoft doesn't immediately trust it. From a security and reliability perspective, Microsoft needs to:

1. **Verify the SBC is reachable** at the configured FQDN
2. **Authenticate the SBC's identity** via TLS certificates
3. **Continuously monitor health** to avoid routing calls to dead endpoints

This is implemented using **SIP OPTIONS** messages over **mutual TLS (mTLS)**.

#### What is SIP OPTIONS?

SIP OPTIONS is a lightweight "ping" mechanism defined in [RFC 3261](https://datatracker.ietf.org/doc/html/rfc3261#section-11.1). Unlike INVITE (which initiates a call), OPTIONS simply queries the capabilities of a SIP endpoint:

```
OPTIONS sip:sbc.example.com SIP/2.0
Via: SIP/2.0/TLS sip.pstnhub.microsoft.com:5061
From: <sip:sip.pstnhub.microsoft.com>;tag=abc123
To: <sip:sbc.example.com>
Call-ID: health-check-xyz@microsoft.com
CSeq: 1 OPTIONS
Max-Forwards: 70
Content-Length: 0
```

The SBC should respond with `200 OK`:

```
SIP/2.0 200 OK
Via: SIP/2.0/TLS sip.pstnhub.microsoft.com:5061
From: <sip:sip.pstnhub.microsoft.com>;tag=abc123
To: <sip:sbc.example.com>;tag=def456
Call-ID: health-check-xyz@microsoft.com
CSeq: 1 OPTIONS
Content-Length: 0
```

This exchange confirms the SBC is alive and responding to SIP traffic.

#### The mTLS Requirement

Microsoft requires **TLS 1.2+** for all Direct Routing connections. But critically, this isn't just server-side TLS—it's **mutual TLS (mTLS)**, where both parties authenticate:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    TLS Handshake (Simplified)                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  SBC                              Microsoft (sip.pstnhub.microsoft.com)
│   │                                       │                         │
│   │──── ClientHello ─────────────────────►│                         │
│   │                                       │                         │
│   │◄─── ServerHello + ServerCertificate ──│                         │
│   │◄─── CertificateRequest ───────────────│  ← Microsoft asks for   │
│   │                                       │    SBC's certificate    │
│   │                                       │                         │
│   │──── ClientCertificate ───────────────►│  ← SBC presents its     │
│   │                                       │    certificate          │
│   │──── CertificateVerify ───────────────►│                         │
│   │──── Finished ────────────────────────►│                         │
│   │                                       │                         │
│   │◄─── Finished ─────────────────────────│                         │
│   │                                       │                         │
│   │═══════ Encrypted SIP Channel ═════════│                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Key insight:** When Microsoft sends `CertificateRequest`, your SBC must present a valid certificate. If it doesn't (or presents an invalid/self-signed one), Microsoft rejects the connection with:

```
SSL3_AL_FATAL: certificate unknown (sni: unknown)
```

This is why self-signed certificates don't work—Microsoft's servers only trust certificates from well-known Certificate Authorities (CAs).

#### Bidirectional Health Checks

Here's where it gets interesting: Microsoft doesn't just send OPTIONS to you—**you must also send OPTIONS to Microsoft**. The flow is bidirectional:

```
┌─────────────────────────────────────────────────────────────────────┐
│                   Bidirectional SIP OPTIONS                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Your SBC (sbc.example.com)         Microsoft (sip.pstnhub.microsoft.com)
│         │                                      │                    │
│         │                                      │                    │
│    ┌────┴────┐   OPTIONS (every 60s)    ┌──────┴──────┐             │
│    │  Timer  │─────────────────────────►│  SIP Proxy  │             │
│    └────┬────┘                          └──────┬──────┘             │
│         │◄────────── 200 OK ───────────────────│                    │
│         │                                      │                    │
│         │                                      │                    │
│    ┌────┴────┐   OPTIONS (periodic)     ┌──────┴──────┐             │
│    │   SIP   │◄─────────────────────────│  Health     │             │
│    │  Stack  │                          │  Monitor    │             │
│    └────┬────┘                          └──────┬──────┘             │
│         │───────── 200 OK ─────────────────────►│                    │
│         │                                      │                    │
│                                                                     │
│    ═══════════════ Both directions working ═════════════════════    │
│                                                                     │
│                    ↓ Teams Admin Center shows:                      │
│                    ✓ TLS Connectivity: Active                       │
│                    ✓ SIP Options: Active                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Why bidirectional?** Microsoft's SIP proxy (`sip.pstnhub.microsoft.com`) may not initiate connections to your SBC until you've proven you can reach it first. This is a bootstrap problem:

1. **SBC sends OPTIONS → Microsoft**: Proves outbound connectivity and mTLS capability
2. **Microsoft responds 200 OK**: Acknowledges your SBC is alive
3. **Microsoft sends OPTIONS → SBC**: Verifies inbound connectivity
4. **SBC responds 200 OK**: Confirms full bidirectional health

Only when both directions succeed does the Teams Admin Center show "Active" status.

#### The Connection State Machine

The SBC's connection status follows a state machine:

```
                    ┌──────────────────┐
                    │                  │
           ┌───────►│     Inactive     │◄─────────┐
           │        │  (no traffic)    │          │
           │        └────────┬─────────┘          │
           │                 │                    │
           │                 │ SBC sends OPTIONS  │
           │                 │ over mTLS          │
           │                 ▼                    │
           │        ┌──────────────────┐          │
    OPTIONS│        │                  │          │ TLS handshake
    timeout│        │   Connecting     │          │ fails / cert
    or     │        │  (outbound OK)   │          │ invalid
    failure│        └────────┬─────────┘          │
           │                 │                    │
           │                 │ Microsoft sends    │
           │                 │ OPTIONS back       │
           │                 ▼                    │
           │        ┌──────────────────┐          │
           │        │                  │──────────┘
           └────────│     Active       │
                    │ (bidirectional)  │
                    │                  │◄─────────┐
                    └────────┬─────────┘          │
                             │                    │
                             │ Keep-alive OPTIONS │
                             │ every 60 seconds   │
                             └────────────────────┘
```

If OPTIONS messages stop flowing (e.g., SBC goes down, network issue), Microsoft marks the SBC as inactive within 15 minutes.

#### Implementation in Kamailio

For a self-hosted Kamailio SBC, you need to:

1. **Accept incoming OPTIONS** (respond 200 OK):

```kamailio
request_route {
    # ... other routing logic ...
    
    if (is_method("OPTIONS")) {
        xlog("L_INFO", "OPTIONS request - responding 200 OK\n");
        sl_send_reply("200", "OK");
        exit;
    }
}
```

2. **Send outgoing OPTIONS** (using `rtimer` + `uac` modules):

```kamailio
# Load modules
loadmodule "rtimer.so"
loadmodule "uac.so"

# Timer fires every 60 seconds
modparam("rtimer", "timer", "name=ms_options;interval=60;mode=1;")
modparam("rtimer", "exec", "timer=ms_options;route=SEND_MS_OPTIONS")

route[SEND_MS_OPTIONS] {
    $uac_req(method) = "OPTIONS";
    $uac_req(ruri) = "sip:sip.pstnhub.microsoft.com:5061;transport=tls";
    $uac_req(furi) = "sip:sbc.example.com";
    $uac_req(turi) = "sip:sip.pstnhub.microsoft.com";
    $uac_req(callid) = "keepalive-" + $Ts + "@sbc.example.com";
    $uac_req(hdrs) = "Contact: <sip:sbc.example.com;transport=tls>\r\n";
    $uac_req(evroute) = 1;
    uac_req_send();
}
```

3. **Configure mTLS for outbound connections** (in `tls.cfg`):

```ini
[client:default]
method = TLSv1.2
verify_certificate = no      # We trust Microsoft's cert
require_certificate = no
private_key = /etc/kamailio/tls/server.key    # OUR key
certificate = /etc/kamailio/tls/server.pem    # OUR cert (presented to Microsoft)
ca_list = /etc/ssl/certs/ca-certificates.crt
```

The critical line is presenting **our certificate** when acting as a TLS client—this is what Microsoft uses to identify our SBC.

#### Why Uncertified SBCs Can Work

Microsoft publishes a list of "certified" SBCs, but certification is primarily about:

1. **Interoperability testing**: The vendor has tested with Microsoft's infrastructure
2. **Support agreements**: Microsoft and the vendor have a support relationship
3. **Feature completeness**: Certified SBCs support all Direct Routing features

However, the **protocol-level requirements** are well-documented and based on open standards:

- SIP over TLS 1.2+ (RFC 3261 + RFC 3263)
- mTLS with a publicly-trusted certificate
- SIP OPTIONS health checks
- Standard SIP INVITE/ACK/BYE for calls

An uncertified SBC like Kamailio can implement all of these correctly. The key is:

| Requirement | Certified SBC | Self-Hosted Kamailio |
|-------------|---------------|----------------------|
| TLS 1.2+ | ✓ Built-in | ✓ Via `tls.so` module |
| Trusted certificate | ✓ Pre-configured | ✓ Via Let's Encrypt |
| SIP OPTIONS (inbound) | ✓ Automatic | ✓ `sl_send_reply("200", "OK")` |
| SIP OPTIONS (outbound) | ✓ Automatic | ✓ Via `rtimer` + `uac` modules |
| mTLS client auth | ✓ Built-in | ✓ Configure in `tls.cfg` |

The Teams Admin Center doesn't check for certification—it checks for **protocol compliance**. If your SBC responds correctly to OPTIONS and maintains mTLS, it works.

#### Debugging Connection Issues

When troubleshooting, check these in order:

1. **DNS resolution**: Can Microsoft resolve your SBC FQDN?
   ```bash
   dig sbc.example.com
   ```

2. **TLS certificate chain**: Is your cert trusted and not expired?
   ```bash
   openssl s_client -connect sbc.example.com:5061 -servername sbc.example.com
   ```

3. **Outbound OPTIONS**: Are you sending health checks?
   ```bash
   # Check Kamailio logs for:
   # "=== Sending SIP OPTIONS to Microsoft Teams ==="
   # "Microsoft replied with status code: 200"
   ```

4. **Inbound OPTIONS**: Is Microsoft reaching you?
   ```bash
   # Check Kamailio logs for:
   # "=== Incoming OPTIONS from sip:sip-du-a-us.pstnhub.microsoft.com..."
   ```

5. **mTLS client certificate**: Are you presenting your cert?
   ```bash
   # Check for errors like:
   # "SSL3_AL_FATAL: certificate unknown"
   # This means client cert is missing or invalid
   ```

#### Summary

The SBC-to-Teams connection is a three-layer verification:

| Layer | Protocol | What's Verified |
|-------|----------|-----------------|
| **Transport** | TLS 1.2+ | Encryption + SBC's certificate identity |
| **Network** | TCP/5061 | Bidirectional reachability |
| **Application** | SIP OPTIONS | SBC is alive and SIP-capable |

Once all three layers pass bidirectionally, calls can flow. The beauty of this design is that it's entirely based on open standards—no proprietary protocols or "magic" from certified vendors.

---

## Prerequisites

- [ ] **Microsoft 365 License** with Teams Phone capabilities (see licensing below)
- [ ] **LiveKit Cloud account** with SIP enabled
- [ ] **Domain** you control (for SBC FQDN)
- [ ] **Admin access** to:
  - Microsoft 365 Admin Center
  - Teams Admin Center
  - DNS for your domain
- [ ] **SSL Certificate** for your SBC domain

### ⚠️ Teams Phone Licensing (Required)

**Standard M365 subscriptions (Business Basic/Standard/Premium) do NOT include Teams Phone.** You need one of:

| Option | Cost | Notes |
|--------|------|-------|
| **Teams Phone add-on** | ~$8/user/month | Add to existing M365 plan |
| **Microsoft 365 E5** | ~$57/user/month | Includes Teams Phone |
| **Teams Phone Standard** | ~$8/user/month | Standalone voice license |

**What's included with Teams Phone:**
- Direct Routing (connect your own SIP provider)
- Resource Accounts for voice features
- Call queues and auto attendants
- PSTN calling capabilities

**What's free (but requires Teams Phone in tenant):**
- Microsoft Teams Phone Resource Account license (for resource accounts)

> **How to check:** Go to [Teams Admin Center](https://admin.teams.microsoft.com) → **Voice** → **Resource accounts**. If you see "Try Free for 30 days", your tenant doesn't have Teams Phone.

> **Tip:** Use the 30-day trial to test the integration before committing to the add-on.

---

## Step 1: Choose an SBC Provider

You need a Session Border Controller (SBC) to bridge Teams and SIP.

> ### ⚠️ Why You Can't Connect Teams Directly to LiveKit
>
> Microsoft Teams requires the SBC domain to be **verified in your M365 tenant**. You can only verify domains you own. Since LiveKit's domain (`*.sip.livekit.cloud`) is owned by LiveKit, you cannot add it directly to Teams.
>
> **Solution:** Use an intermediary SBC with a domain you control:
> ```
> Teams → sbc.yourdomain.com (verified) → forwards SIP → LiveKit
> ```

Options:

### Cloud SBC Providers (Recommended for Getting Started)

| Provider | Starting Cost | Setup Time | Notes |
|----------|---------------|------------|-------|
| **AudioCodes Live** | ~$50/month | 1-2 hours | Teams certified, easy UI |
| **Ribbon SBC SWe Lite** | ~$100/month | 2-3 hours | More enterprise features |
| **Oracle Cloud SBC** | ~$150/month | 3-4 hours | High availability options |

### Using Twilio as SBC (If You Already Have Twilio)

If you already use Twilio, you can use **Twilio Elastic SIP Trunking** as your SBC:

```
Teams → Direct Routing → Twilio SIP → LiveKit SIP
```

This reuses your existing Twilio infrastructure.

### Self-Hosted SBC on Cloud VM (Cost-Effective)

Run your own SBC on a small cloud VM for minimal cost:

```
Teams → sbc.yourdomain.com (your VM) → LiveKit SIP
```

| Component | Monthly Cost |
|-----------|--------------|
| GCP e2-micro VM | ~$6-8 |
| Static IP | ~$3 |
| Disk (10GB) | ~$1 |
| **Total** | **~$12-15/month** |

**Software options (open source, free):**
- **Kamailio** - Pure SIP proxy, lightweight
- **FreeSWITCH** - Full-featured PBX
- **Odin** - Modern WebRTC/SIP bridge

**Pros:** Cheapest long-term, full control
**Cons:** You maintain it, initial setup time

### On-Premises SBC (For Enterprise)

| Provider | Notes |
|----------|-------|
| AudioCodes Mediant | Hardware appliance |
| Ribbon SBC 1000/2000 | Hardware or VM |
| Oracle Enterprise SBC | High scale |

### Cost Comparison Summary

| Option | Monthly Cost | Effort | Best For |
|--------|--------------|--------|----------|
| **Self-hosted (GCP/AWS)** | ~$15 | High | Cost-conscious, technical teams |
| **AudioCodes Live** | ~$50 | Low | Quick setup, managed |
| **Ribbon SBC** | ~$100 | Low | Enterprise features |
| **Twilio** | Per-minute | Medium | Already using Twilio |

> **Recommendation:** Start with **AudioCodes Live** for testing, or **self-hosted Kamailio** on GCP if you want to minimize costs.

---

## Step 2: Set Up Your SBC

### Option A: AudioCodes Live (Cloud)

1. Sign up at [AudioCodes Live](https://live.audiocodes.com)
2. Create a new tenant
3. Add a SIP Trunk:
   - **Name:** `LiveKit-Trunk`
   - **Destination:** Your LiveKit SIP URI (e.g., `your-trunk.sip.livekit.cloud`)
   - **Transport:** TLS
   - **Port:** 5061

4. Add Teams Direct Routing connection:
   - Follow AudioCodes wizard for Microsoft Teams integration
   - Download the certificate for Teams

5. Note your SBC FQDN: `your-tenant.audiocodes.live`

### Option B: Twilio Elastic SIP Trunking

1. Go to [Twilio Console](https://console.twilio.com) → **Elastic SIP Trunking**
2. Create a new SIP Trunk:
   - **Friendly Name:** `Teams-to-LiveKit`
   
3. Configure Origination (Teams → Twilio):
   - **Origination SIP URI:** Your Twilio SIP domain
   
4. Configure Termination (Twilio → LiveKit):
   - **Termination SIP URI:** `sip:your-trunk.sip.livekit.cloud`
   - **Transport:** TLS

5. Note your Twilio SIP domain: `your-trunk.pstn.twilio.com`

### Option C: Self-Hosted SBC (Kamailio on GCP)

If self-hosting, ensure:
- Public IP address with DNS A record
- Valid TLS certificate (not self-signed)
- Ports open: 5061 (SIP/TLS), 10000-60000 (RTP/UDP)
- Configure routing rules: Teams → LiveKit SIP

> **Detailed Setup:** See [Appendix A: Self-Hosted Kamailio SBC Setup](#appendix-a-self-hosted-kamailio-sbc-setup) for complete step-by-step instructions.

---

## Step 3: Configure Teams Direct Routing

### 3.1 Verify Your SBC Domain

1. Go to [Microsoft 365 Admin Center](https://admin.microsoft.com)
2. Navigate to **Settings** → **Domains**
3. Click **Add domain**
4. Enter your SBC FQDN (e.g., `sbc.yourdomain.com`)
5. Verify ownership via DNS TXT record
6. **Important:** Only verify, don't set up email

### 3.2 Add SBC to Teams

1. Go to [Teams Admin Center](https://admin.teams.microsoft.com)
2. Navigate to **Voice** → **Direct Routing**
3. Click **Add** button
4. Configure:

```
FQDN: sbc.yourdomain.com (or your cloud SBC FQDN)
Enabled: Yes
SIP signaling port: 5061
Bypass: Off
Media bypass: Off (unless you know you need it)
```

5. Click **Save**
6. Wait for status to show "Online" (may take 5-15 minutes)

### 3.3 Verify Connection

Check the SBC status shows a green checkmark. If not:
- Verify DNS resolution
- Check TLS certificate is valid
- Ensure firewall allows traffic from Microsoft IPs

---

## Step 4: Create Resource Account

A Resource Account is a virtual identity in Teams that users can call. No phone number required.

### 4.1 Create the Resource Account

1. Go to [Teams Admin Center](https://admin.teams.microsoft.com)
2. Navigate to **Voice** → **Resource accounts**
3. Click **+ Add**
4. Configure:

```
Display name: AI Assistant
Username: ai-assistant@yourdomain.com
Resource account type: Auto attendant
```

5. Click **Save**

### 4.2 Assign License (Required)

The Resource Account needs a **Microsoft Teams Phone Resource Account** license (free):

1. Go to [Microsoft 365 Admin Center](https://admin.microsoft.com)
2. Navigate to **Users** → **Active users**
3. Find `ai-assistant@yourdomain.com`
4. Click **Licenses and apps**
5. Assign **Microsoft Teams Phone Resource Account** license
6. Save changes

> **Note:** This license is free but must be assigned for voice routing to work.

---

## Step 5: Configure Voice Routing

### 5.1 Install Teams PowerShell Module

```powershell
# Install the module (run as Administrator)
Install-Module -Name MicrosoftTeams -Force

# Connect to Teams
Connect-MicrosoftTeams
```

### 5.2 Create PSTN Usage

```powershell
# Create a PSTN usage for LiveKit routing
Set-CsOnlinePstnUsage -Identity Global -Usage @{Add="LiveKit-Usage"}
```

### 5.3 Create Voice Route

```powershell
# Create a voice route that sends calls to your SBC
New-CsOnlineVoiceRoute -Identity "LiveKit-Route" `
    -NumberPattern ".*" `
    -OnlinePstnGatewayList "sbc.yourdomain.com" `
    -Priority 1 `
    -OnlinePstnUsages "LiveKit-Usage"
```

### 5.4 Create Voice Routing Policy

```powershell
# Create a policy that uses the LiveKit route
New-CsOnlineVoiceRoutingPolicy -Identity "LiveKit-Policy" `
    -OnlinePstnUsages "LiveKit-Usage"
```

### 5.5 Assign Policy to Resource Account

```powershell
# Enable the resource account for Enterprise Voice
Set-CsPhoneNumberAssignment -Identity "ai-assistant@yourdomain.com" `
    -EnterpriseVoiceEnabled $true

# Assign the voice routing policy
Grant-CsOnlineVoiceRoutingPolicy -Identity "ai-assistant@yourdomain.com" `
    -PolicyName "LiveKit-Policy"
```

### 5.6 Verify Configuration

```powershell
# Check the resource account configuration
Get-CsOnlineUser -Identity "ai-assistant@yourdomain.com" | 
    Select-Object DisplayName, EnterpriseVoiceEnabled, OnlineVoiceRoutingPolicy
```

Expected output:
```
DisplayName   EnterpriseVoiceEnabled  OnlineVoiceRoutingPolicy
-----------   ----------------------  ------------------------
AI Assistant  True                    LiveKit-Policy
```

---

## Step 6: Configure LiveKit SIP Trunk

### 6.1 Create Inbound SIP Trunk in LiveKit

Using the LiveKit CLI or API:

```bash
# Using LiveKit CLI
lk sip inbound create \
    --name "Teams-Direct-Routing" \
    --numbers "+*" \
    --allowed-addresses "YOUR_SBC_IP_ADDRESS"
```

Or via Python (similar to your existing code):

```python
from livekit.api import LiveKitAPI, SIPInboundTrunkInfo, CreateSIPInboundTrunkRequest

async def create_teams_sip_trunk():
    lkapi = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    
    sip_trunk = SIPInboundTrunkInfo(
        name="Teams-Direct-Routing",
        # Accept calls from any number (Resource Account doesn't have a real number)
        numbers=["+*"],
        # Restrict to your SBC's IP for security
        allowed_addresses=["YOUR_SBC_IP_ADDRESS"],
    )
    
    request = CreateSIPInboundTrunkRequest(trunk=sip_trunk)
    result = await lkapi.sip.create_sip_inbound_trunk(request)
    print(f"Created SIP trunk: {result.sip_trunk_id}")
    
    await lkapi.aclose()
    return result
```

### 6.2 Configure SRTP Encryption (Required for Teams)

**Critical:** Microsoft Teams requires SRTP (encrypted audio). Without this, calls will disconnect immediately.

In the LiveKit Cloud Dashboard, configure your SIP trunk:

| Setting | Value | Why |
|---------|-------|-----|
| **Media Encryption** | `require` | Teams mandates SRTP for Direct Routing |
| **Include Headers** | `All headers` | Passes caller identity (P-Asserted-Identity, Diversion) |

After enabling, LiveKit's SDP responses will include:
```
m=audio 58004 RTP/SAVP 9 101
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:randomkey...
```

The `RTP/SAVP` (instead of `RTP/AVP`) indicates secure RTP is enabled.

### 6.3 Note Your LiveKit SIP URI

Your LiveKit SIP trunk URI format:
```
sip:+{number}@{your-project}.sip.livekit.cloud
```

Configure your SBC to send calls to this URI.

---

## Step 7: Update Your Application

### 7.1 Add Teams Webhook Handler

Create a webhook to handle incoming Teams calls (similar to your Twilio webhook):

```python
# In your views.py or create a new teams_views.py

@router.post("/teams/call")
async def teams_call_webhook(request: Request):
    """
    Handle incoming calls from Teams via SIP.
    This is called when LiveKit receives a SIP call from your SBC.
    """
    data = await request.json()
    
    # Extract call info from SIP headers
    caller_id = data.get("from_number", "")  # Teams user's identity
    sip_trunk_id = data.get("sip_trunk_id", "")
    room_name = data.get("room_name", "")
    
    print(f"Teams call received from {caller_id} in room {room_name}")
    
    # Build context and dispatch agent (similar to your phone webhook)
    context = build_webhook_context(
        channel="teams",
        destination="teams",
        sender=caller_id,
        validate_contact=False,
        ensure_job=True,
    )
    
    assistant_id = context["assistant"]["assistant_id"]
    
    # Publish to Pub/Sub for your agent to handle
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}"
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    
    pubsub_client.publish(
        topic_path,
        json.dumps({
            "thread": "teams_call",
            "event": {
                "contacts": context["contacts"],
                "caller_id": caller_id,
                "livekit_room": room_name,
                "assistant_id": assistant_id,
                "timestamp": int(time.time() * 1000),
            },
        }).encode("utf-8"),
    )
    
    return {"status": "ok"}
```

### 7.2 Configure LiveKit SIP Dispatch

Set up LiveKit to dispatch your agent when a SIP call arrives:

```python
# Configure SIP dispatch rules
from livekit.api import SIPDispatchRuleInfo, CreateSIPDispatchRuleRequest

async def setup_teams_dispatch():
    lkapi = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    
    # Create dispatch rule to auto-create rooms and dispatch agents
    dispatch_rule = SIPDispatchRuleInfo(
        name="Teams-Calls",
        trunk_ids=["your-teams-sip-trunk-id"],
        rule={
            "dispatchRuleDirect": {
                "roomName": "teams-${caller.number}",  # Dynamic room name
                "pin": "",  # No PIN required
            }
        },
        # Optional: webhook for custom handling
        metadata=json.dumps({"source": "teams"}),
    )
    
    await lkapi.sip.create_sip_dispatch_rule(
        CreateSIPDispatchRuleRequest(rule=dispatch_rule)
    )
    
    await lkapi.aclose()
```

---

## Step 8: Test the Integration

### 8.1 Test SBC Connectivity

```bash
# Test SIP connectivity to your SBC (replace with your SBC address)
nc -zv sbc.yourdomain.com 5061
```

### 8.2 Test from Teams Client

1. Open Microsoft Teams desktop or mobile app
2. Go to **Calls**
3. In the dial pad or search, enter: `ai-assistant@yourdomain.com`
4. Click **Call**

### 8.3 Verify Call Flow

Check these in order:

1. **Teams Admin Center** → Voice → Direct Routing → Check SBC status
2. **SBC Dashboard** → Check call logs for incoming call
3. **LiveKit Dashboard** → Check for new room creation
4. **Your Application Logs** → Check webhook received call

### 8.4 Test Audio

Once connected:
1. Speak into Teams client
2. Verify audio reaches your LiveKit agent
3. Verify agent response plays back in Teams

---

## Troubleshooting

### SBC Not Showing "Online" in Teams

**Symptoms:** SBC status shows offline or unknown in Teams Admin Center

**Solutions:**
1. Verify DNS: `nslookup sbc.yourdomain.com`
2. Check TLS certificate is valid and not self-signed
3. Ensure port 5061 is open: `telnet sbc.yourdomain.com 5061`
4. Verify domain is verified in M365 Admin Center
5. Wait 15-30 minutes for propagation

### Calls Fail with "Call cannot be completed"

**Symptoms:** Teams shows error when calling Resource Account

**Solutions:**
1. Verify Resource Account has **Teams Phone Resource Account** license
2. Check Enterprise Voice is enabled:
   ```powershell
   Get-CsOnlineUser -Identity "ai-assistant@yourdomain.com" | Select EnterpriseVoiceEnabled
   ```
3. Verify voice routing policy is assigned
4. Check voice route number pattern matches

### Calls Reach SBC but Not LiveKit

**Symptoms:** SBC logs show call, but LiveKit dashboard shows nothing

**Solutions:**
1. Check SBC → LiveKit SIP trunk configuration
2. Verify LiveKit SIP trunk allows your SBC IP:
   ```python
   # Check allowed_addresses includes your SBC
   allowed_addresses=["YOUR_SBC_IP"]
   ```
3. Check SBC is sending to correct LiveKit SIP URI
4. Verify TLS is working between SBC and LiveKit

### No Audio / One-Way Audio

**Symptoms:** Call connects but no audio or only one direction works

**Solutions:**
1. Check RTP ports are open (UDP 10000-60000)
2. Verify SBC NAT traversal settings
3. Check LiveKit room has correct audio tracks
4. Verify agent is publishing audio correctly

### Call Disconnects Immediately (~1 second)

**Symptoms:** Call appears to connect briefly, then disconnects with `CLIENT_INITIATED` in LiveKit logs. LiveKit dashboard shows `0 bps` bitrate.

**Root Cause:** Microsoft Teams requires **SRTP (Secure RTP)** for all Direct Routing calls. If LiveKit responds with plain `RTP/AVP` instead of `RTP/SAVP`, Teams cannot send encrypted audio.

**Solution:** Enable SRTP encryption on your LiveKit SIP trunk:

1. Go to LiveKit Cloud Dashboard → SIP → Your Inbound Trunk
2. Set **Media Encryption** to `require` (or equivalent)
3. Set **Include Headers** to `All headers`

After enabling, the SDP in the 200 OK should show:
```
m=audio 58004 RTP/SAVP 9 101     ← Note: SAVP not AVP
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:...
```

### Media Bypass Configuration

**What is Media Bypass?**

Media Bypass allows RTP audio to flow directly between the Teams client and LiveKit, bypassing Microsoft's Media Processors:

```
Without Media Bypass:
Teams Client → Microsoft Media Processors → SBC → LiveKit

With Media Bypass (Recommended for this setup):
Teams Client ──────────── RTP Audio ──────────→ LiveKit
      │                                              │
      └── SIP Signaling → SBC → SIP Signaling ───────┘
```

**Why it's needed:** Your SBC is signaling-only (no media relay). Without Media Bypass, Microsoft sends RTP to your SBC, which has nowhere to forward it.

**How to enable:**

```powershell
# Enable Media Bypass on your SBC
Set-CsOnlinePSTNGateway -Identity "sbc.yourdomain.com" -MediaBypass $true

# Verify
Get-CsOnlinePSTNGateway -Identity "sbc.yourdomain.com" | Select-Object Fqdn, MediaBypass
```

**Requirements for Media Bypass to work:**
1. LiveKit must use SRTP (`RTP/SAVP`) - Teams won't send unencrypted audio
2. LiveKit's SDP must contain a public IP address (not internal IPs)
3. No strict firewalls blocking direct Teams → LiveKit UDP traffic

### Call Quality Issues

**Symptoms:** Choppy audio, delays, or dropouts

**Solutions:**
1. Check network latency between SBC and LiveKit
2. Verify SBC has sufficient bandwidth
3. Consider geographic proximity of SBC to LiveKit region
4. Check for packet loss: `mtr sbc.yourdomain.com`

---

## Known Issues

### SRTP Crypto Tag Mismatch (New Teams Desktop Client)

**Status:** Intermittent issue affecting new Teams desktop client (as of late 2024)

**Symptoms:** 
- Calls work from Teams **web client** (teams.microsoft.com)
- Calls fail from new Teams **desktop client** (disconnects after ~0.8 seconds)
- LiveKit logs show `CLIENT_INITIATED` disconnect
- SBC logs show `a=crypto:2` in the 200 OK response

**Root Cause:**

When Teams offers multiple SRTP crypto options:
```
a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:keyAAAA...
a=crypto:2 AES_CM_128_HMAC_SHA1_32 inline:keyBBBB...
```

LiveKit randomly selects one to respond with. The new Teams desktop client appears to have a bug where it only works when LiveKit selects `crypto:1`. If LiveKit responds with `crypto:2`, the call fails.

| Teams Version | Crypto Tag in Response | Result |
|---------------|------------------------|--------|
| Web client | `crypto:1` or `crypto:2` | ✅ Works |
| Old desktop | `crypto:1` or `crypto:2` | ✅ Works |
| **New desktop** | `crypto:1` | ✅ Works |
| **New desktop** | `crypto:2` | ❌ Fails |

**Workarounds:**

1. **Use Teams web client** (teams.microsoft.com) - works reliably
2. **Retry calls** - if LiveKit happens to select `crypto:1`, it will work
3. **Report to both parties:**
   - **LiveKit:** Request option to prefer `crypto:1` in SRTP responses
   - **Microsoft:** Report that new Teams desktop doesn't accept `crypto:2` (RFC violation - answerer can choose any offered option)

**Expected Resolution:** Microsoft will likely fix the desktop client bug in a future update, as this violates SIP/SRTP standards where the answerer can legitimately choose any offered crypto suite.

---

## Environment Variables

```bash
# LiveKit Configuration
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-api-key
LIVEKIT_API_SECRET=your-api-secret
LIVEKIT_SIP_URI=your-project.sip.livekit.cloud

# SBC Configuration
SBC_ADDRESS=sbc.yourdomain.com
SBC_IP=203.0.113.50  # Your SBC's public IP

# Teams Resource Account
TEAMS_RESOURCE_ACCOUNT=ai-assistant@yourdomain.com
```

---

## Useful Commands Reference

### Teams PowerShell

```powershell
# Connect to Teams
Connect-MicrosoftTeams

# List all Direct Routing SBCs
Get-CsOnlinePSTNGateway

# List voice routes
Get-CsOnlineVoiceRoute

# List voice routing policies
Get-CsOnlineVoiceRoutingPolicy

# Check user's voice settings
Get-CsOnlineUser -Identity "user@domain.com" | Select *voice*

# Test voice routing
Test-CsOnlineUserVoiceRouting -Identity "ai-assistant@yourdomain.com" -TargetNumber "+15551234567"
```

### LiveKit CLI

```bash
# List SIP trunks
lk sip inbound list

# List SIP dispatch rules
lk sip dispatch list

# List active rooms
lk room list

# Check room participants
lk room list-participants --room "room-name"
```

---

## Useful Links

### Microsoft Documentation
- [Plan Direct Routing](https://learn.microsoft.com/en-us/microsoftteams/direct-routing-plan)
- [Configure Direct Routing](https://learn.microsoft.com/en-us/microsoftteams/direct-routing-configure)
- [Resource Accounts](https://learn.microsoft.com/en-us/microsoftteams/manage-resource-accounts)
- [Teams PowerShell Reference](https://learn.microsoft.com/en-us/powershell/module/teams)

### LiveKit Documentation
- [LiveKit SIP Overview](https://docs.livekit.io/sip/)
- [SIP Trunking Guide](https://docs.livekit.io/sip/quickstart/)
- [SIP Dispatch Rules](https://docs.livekit.io/sip/dispatch-rules/)

### SBC Documentation
- [AudioCodes with Teams](https://www.audiocodes.com/solutions-products/solutions/microsoft-solutions/direct-routing)
- [Twilio Elastic SIP](https://www.twilio.com/docs/sip-trunking)

---

## Next Steps

1. **Set up your SBC** with a provider
2. **Configure Direct Routing** in Teams Admin Center
3. **Create the Resource Account** for users to call
4. **Configure LiveKit SIP trunk** to receive calls
5. **Test end-to-end** with a Teams call
6. **Integrate with your agent** pipeline

For the email integration setup, see [OUTLOOK_SETUP_GUIDE.md](./OUTLOOK_SETUP_GUIDE.md).

---

## Appendix A: Self-Hosted Kamailio SBC Setup

This appendix provides detailed instructions for setting up a Kamailio SBC on Google Cloud Platform to bridge Microsoft Teams Direct Routing with LiveKit SIP.

### A.1 Architecture Overview

```
Teams User
    │
    ▼ (SIP/TLS on port 5061)
┌─────────────────────────┐
│   sbc.yourdomain.com    │  ← GCP VM with Kamailio
│   (your verified domain)│
└─────────────────────────┘
    │
    ▼ (SIP on port 5060)
┌─────────────────────────┐
│   LiveKit SIP Cloud     │
│   *.sip.livekit.cloud   │
└─────────────────────────┘
    │
    ▼
┌─────────────────────────┐
│   LiveKit Room          │
│   + Your AI Agent       │
└─────────────────────────┘
```

### A.2 Prerequisites

- [ ] GCP account with billing enabled
- [ ] Domain you control (e.g., `unify.ai`)
- [ ] `gcloud` CLI installed and authenticated
- [ ] LiveKit Cloud account with SIP enabled

### A.3 Step 1: Create GCP Infrastructure

#### 1.1 Create a Static IP

```bash
# Set your project
gcloud config set project YOUR_PROJECT_ID

# Create static IP
gcloud compute addresses create kamailio-sbc-ip \
    --region=us-central1 \
    --description="Static IP for Kamailio SBC"

# Get the IP address
gcloud compute addresses describe kamailio-sbc-ip \
    --region=us-central1 \
    --format="get(address)"
```

Note this IP - you'll need it for DNS configuration.

#### 1.2 Create Firewall Rules

```bash
# Allow SIP signaling (TLS on 5061, UDP/TCP on 5060)
gcloud compute firewall-rules create allow-sip-signaling \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:5060,tcp:5061,udp:5060 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=sip-server \
    --description="Allow SIP signaling for Teams Direct Routing"

# Allow RTP media (UDP ports for audio)
gcloud compute firewall-rules create allow-rtp-media \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=udp:10000-60000 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=sip-server \
    --description="Allow RTP media for SIP calls"
```

#### 1.3 Create the VM

```bash
gcloud compute instances create kamailio-sip-proxy \
    --zone=us-central1-f \
    --machine-type=e2-micro \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --boot-disk-size=10GB \
    --address=kamailio-sbc-ip \
    --tags=sip-server \
    --metadata=startup-script='#!/bin/bash
apt-get update
apt-get install -y docker.io docker-compose certbot
systemctl enable docker
systemctl start docker
usermod -aG docker $(whoami)'
```

### A.4 Step 2: Configure DNS

Create an A record for your SBC subdomain pointing to the static IP:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A | sbc | YOUR_STATIC_IP | 300 |

This creates `sbc.yourdomain.com` → `YOUR_STATIC_IP`

Verify DNS propagation:
```bash
nslookup sbc.yourdomain.com
dig sbc.yourdomain.com
```

### A.5 Step 3: Set Up TLS Certificates

SSH into your VM:
```bash
gcloud compute ssh kamailio-sip-proxy --zone=us-central1-f
```

Run Certbot to get Let's Encrypt certificates:
```bash
sudo certbot certonly --standalone \
    -d sbc.yourdomain.com \
    --non-interactive \
    --agree-tos \
    --email your-email@yourdomain.com
```

Create the TLS directory and copy certificates:
```bash
mkdir -p ~/sbc-proxy/tls

# Copy certificates to the right location
sudo cp /etc/letsencrypt/live/sbc.yourdomain.com/fullchain.pem ~/sbc-proxy/tls/server.pem
sudo cp /etc/letsencrypt/live/sbc.yourdomain.com/privkey.pem ~/sbc-proxy/tls/server.key
sudo cp /etc/letsencrypt/live/sbc.yourdomain.com/chain.pem ~/sbc-proxy/tls/ca.pem

# Fix permissions
sudo chown -R $USER:$USER ~/sbc-proxy/tls
chmod 600 ~/sbc-proxy/tls/server.key
```

### A.6 Step 4: Create Kamailio Configuration

Create the project directory:
```bash
mkdir -p ~/sbc-proxy
cd ~/sbc-proxy
```

#### 4.1 Create `kamailio.cfg`

```bash
cat > kamailio.cfg << 'EOF'
#!KAMAILIO

# Enable TLS for Teams (REQUIRED - Teams only connects via TLS)
#!define WITH_TLS

####### Global Parameters #########

# Set to your SBC domain (must match what's verified in M365)
#!substdef "!MY_DOMAIN!sbc.yourdomain.com!g"

# Set to your LiveKit SIP endpoint
#!substdef "!LIVEKIT_SIP!your-project.sip.livekit.cloud!g"
#!substdef "!LIVEKIT_PORT!5060!g"

debug=2
log_stderror=yes
log_facility=LOG_LOCAL0

fork=yes
children=4

auto_aliases=no

# Ports - advertise the public SBC domain so Via headers are correct
# Without advertise, Via headers may contain 0.0.0.0 which breaks routing
listen=udp:0.0.0.0:5060 advertise sbc.yourdomain.com:5060
listen=tcp:0.0.0.0:5060 advertise sbc.yourdomain.com:5060
listen=tls:0.0.0.0:5061 advertise sbc.yourdomain.com:5061

# Enable TLS (required for Teams)
enable_tls=1

# Aliases
alias=MY_DOMAIN

####### Modules Section ########

loadmodule "tm.so"
loadmodule "sl.so"
loadmodule "rr.so"
loadmodule "pv.so"
loadmodule "maxfwd.so"
loadmodule "textops.so"
loadmodule "siputils.so"
loadmodule "xlog.so"
loadmodule "sanity.so"
loadmodule "path.so"

#!ifdef WITH_TLS
loadmodule "tls.so"
#!endif

####### Module Parameters ########

modparam("rr", "enable_full_lr", 1)
modparam("rr", "append_fromtag", 1)

#!ifdef WITH_TLS
modparam("tls", "config", "/etc/kamailio/tls.cfg")
#!endif

####### Routing Logic ########

request_route {
    xlog("L_INFO", "=== Incoming $rm from $fu to $ru ($si:$sp) ===\n");
    
    if (!mf_process_maxfwd_header("10")) {
        sl_send_reply("483", "Too Many Hops");
        exit;
    }

    if (!sanity_check("1511", "7")) {
        xlog("L_WARN", "Malformed SIP message from $si:$sp\n");
        exit;
    }

    if (is_method("OPTIONS")) {
        sl_send_reply("200", "OK");
        exit;
    }

    # Handle requests within dialog (sequential requests)
    if (has_totag()) {
        # Try loose routing first (uses Route headers from Record-Route)
        if (loose_route()) {
            xlog("L_INFO", "In-dialog $rm - loose routing\n");
            route(RELAY);
            exit;
        }

        # For ACK and BYE: Since we rewrote Contact to point to our SBC,
        # these requests come to us but need to be forwarded to LiveKit
        if (is_method("ACK|BYE")) {
            xlog("L_INFO", "In-dialog $rm - forwarding to LiveKit\n");
            # Rewrite destination to LiveKit (they have the dialog state)
            $ru = "sip:" + $tU + "@LIVEKIT_SIP:LIVEKIT_PORT";
            xlog("L_INFO", "Forwarding $rm to $ru\n");
            route(RELAY);
            exit;
        }

        sl_send_reply("404", "Not Found");
        exit;
    }

    if (is_method("CANCEL")) {
        if (t_check_trans()) {
            route(RELAY);
        }
        exit;
    }

    t_check_trans();

    remove_hf("Route");
    if (is_method("INVITE|SUBSCRIBE")) {
        record_route();
    }

    if (is_method("INVITE")) {
        xlog("L_INFO", "INVITE received - routing to LiveKit\n");
        route(TO_LIVEKIT);
        exit;
    }

    route(RELAY);
}

route[RELAY] {
    if (!t_relay()) {
        sl_reply_error();
    }
    exit;
}

route[TO_LIVEKIT] {
    xlog("L_INFO", "Routing to LiveKit: sip:$rU@LIVEKIT_SIP:LIVEKIT_PORT\n");
    $ru = "sip:" + $rU + "@LIVEKIT_SIP:LIVEKIT_PORT";
    route(RELAY);
}

onreply_route {
    xlog("L_INFO", "=== Reply $rs for $rm ===\n");

    # Remove LiveKit's internal Record-Route headers (they contain internal IPs like 10.x.x.x)
    # Teams can't reach these internal IPs
    if (is_present_hf("Record-Route")) {
        remove_hf("Record-Route");
        xlog("L_INFO", "Removed LiveKit Record-Route headers\n");
        # Add our SBC as Record-Route so dialog stays routed through us
        append_hf("Record-Route: <sip:MY_DOMAIN:5061;transport=tls;lr>\r\n");
    }

    # For 200 OK responses, fix the Contact header
    # LiveKit's Contact points to its internal address which Teams can't reach
    # We need to rewrite it to point to our SBC
    if ($rs == "200") {
        if (is_present_hf("Contact")) {
            remove_hf("Contact");
            # Add our SBC as the Contact - Teams will send BYE here
            append_hf("Contact: <sip:MY_DOMAIN:5061;transport=tls>\r\n");
            xlog("L_INFO", "Rewrote Contact header to SBC\n");
        }
    }
}

failure_route[MANAGE_FAILURE] {
    xlog("L_INFO", "Failure route triggered\n");
    if (t_is_canceled()) {
        exit;
    }
}
EOF
```

**Important:** Replace these placeholders:
- `sbc.yourdomain.com` → your actual SBC domain
- `your-project.sip.livekit.cloud` → your LiveKit SIP URI

#### 4.2 Create `tls.cfg`

```bash
cat > tls.cfg << 'EOF'
# Kamailio TLS Configuration
# Used for secure SIP connections from Microsoft Teams

[server:default]
method = TLSv1.2+
verify_certificate = no
require_certificate = no
private_key = /etc/kamailio/tls/server.key
certificate = /etc/kamailio/tls/server.pem

[client:default]
method = TLSv1.2+
verify_certificate = no
EOF
```

#### 4.3 Create `Dockerfile`

```bash
cat > Dockerfile << 'EOF'
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    kamailio \
    kamailio-tls-modules \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/kamailio/tls

EXPOSE 5060/udp 5060/tcp 5061/tcp

CMD ["kamailio", "-DD", "-E", "-m", "64", "-M", "8"]
EOF
```

#### 4.4 Create `docker-compose.yml`

```bash
cat > docker-compose.yml << 'EOF'
version: '3.8'

services:
  kamailio:
    build: .
    container_name: kamailio-sbc
    ports:
      - "5060:5060/udp"
      - "5060:5060/tcp"
      - "5061:5061/tcp"
    volumes:
      - ./kamailio.cfg:/etc/kamailio/kamailio.cfg:ro
      - ./tls.cfg:/etc/kamailio/tls.cfg:ro
      - ./tls:/etc/kamailio/tls:ro
    restart: unless-stopped
EOF
```

### A.7 Step 5: Build and Run Kamailio

```bash
cd ~/sbc-proxy

# Build and start
docker compose build
docker compose up -d

# Check logs
docker compose logs -f
```

### A.8 Step 6: Verify SBC is Running

From your local machine, test connectivity:

```bash
# Test TLS port (required for Teams)
nc -zv sbc.yourdomain.com 5061

# Test UDP port
nc -zvu sbc.yourdomain.com 5060
```

Expected output:
```
Connection to sbc.yourdomain.com port 5061 [tcp/sip-tls] succeeded!
Connection to sbc.yourdomain.com port 5060 [udp/sip] succeeded!
```

### A.9 Step 7: Verify Domain in Microsoft 365

1. Go to [Microsoft 365 Admin Center](https://admin.microsoft.com)
2. Navigate to **Settings** → **Domains**
3. Click **Add domain**
4. Enter: `sbc.yourdomain.com`
5. Add the TXT record to your DNS as instructed
6. Click **Verify**
7. **Skip** email setup when prompted

### A.10 Step 8: Add SBC to Teams Direct Routing

Using PowerShell:

```powershell
# Install Teams module if needed
Install-Module -Name MicrosoftTeams -Force

# Connect
Connect-MicrosoftTeams

# Add the SBC
New-CsOnlinePSTNGateway -Fqdn sbc.yourdomain.com `
    -SipSignalingPort 5061 `
    -Enabled $true `
    -ForwardPai $true `
    -ForwardCallHistory $true

# Verify
Get-CsOnlinePSTNGateway -Identity sbc.yourdomain.com
```

Or via Teams Admin Center:
1. Go to [Teams Admin Center](https://admin.teams.microsoft.com)
2. Navigate to **Voice** → **Direct Routing**
3. Click **Add**
4. Enter:
   - **FQDN:** `sbc.yourdomain.com`
   - **SIP signaling port:** `5061`
   - **Enabled:** Yes
5. Save and wait for status to show "Online"

### A.11 Certificate Renewal

Let's Encrypt certificates expire after 90 days. Set up auto-renewal:

```bash
# Create renewal script
cat > ~/renew-certs.sh << 'EOF'
#!/bin/bash
certbot renew --quiet
cp /etc/letsencrypt/live/sbc.yourdomain.com/fullchain.pem ~/sbc-proxy/tls/server.pem
cp /etc/letsencrypt/live/sbc.yourdomain.com/privkey.pem ~/sbc-proxy/tls/server.key
cp /etc/letsencrypt/live/sbc.yourdomain.com/chain.pem ~/sbc-proxy/tls/ca.pem
cd ~/sbc-proxy && docker compose restart
EOF

chmod +x ~/renew-certs.sh

# Add to crontab (runs daily at 3am)
(crontab -l 2>/dev/null; echo "0 3 * * * /home/$USER/renew-certs.sh") | crontab -
```

### A.12 Monitoring and Logs

```bash
# View real-time logs
docker compose logs -f

# Check container status
docker compose ps

# Restart if needed
docker compose restart

# View recent logs
docker compose logs --tail=100
```

### A.13 Cost Summary

| Component | Monthly Cost |
|-----------|--------------|
| GCP e2-micro VM | ~$6-8 |
| Static IP | ~$3 |
| Boot disk (10GB) | ~$1 |
| **Total** | **~$10-12/month** |

### A.14 Troubleshooting Self-Hosted SBC

#### TLS Connection Refused (port 5061)

**Symptoms:** `nc -zv sbc.yourdomain.com 5061` fails

**Solutions:**
1. Check TLS is enabled in `kamailio.cfg`:
   ```
   #!define WITH_TLS
   enable_tls=1
   ```
2. Verify certificates exist:
   ```bash
   ls -la ~/sbc-proxy/tls/
   ```
3. Check `tls.cfg` is mounted:
   ```bash
   docker compose exec kamailio cat /etc/kamailio/tls.cfg
   ```
4. View Kamailio logs for TLS errors:
   ```bash
   docker compose logs | grep -i tls
   ```

#### Certificate Errors

**Symptoms:** Kamailio fails to start with "Unable to load certificate"

**Solutions:**
1. Verify certificate paths in `tls.cfg` match mounted locations
2. Check file permissions:
   ```bash
   chmod 644 ~/sbc-proxy/tls/server.pem
   chmod 600 ~/sbc-proxy/tls/server.key
   ```
3. Verify certificate validity:
   ```bash
   openssl x509 -in ~/sbc-proxy/tls/server.pem -text -noout
   ```

#### SBC Shows Offline in Teams

**Symptoms:** Teams Admin Center shows SBC as offline

**Solutions:**
1. Verify DNS resolution from Microsoft's perspective
2. Ensure TLS certificate is from a trusted CA (not self-signed)
3. Check firewall allows Microsoft IP ranges
4. Wait 15-30 minutes for Teams to retry connection

#### Calls Not Reaching LiveKit

**Symptoms:** SBC logs show INVITE but LiveKit doesn't receive it

**Solutions:**
1. Verify LiveKit SIP URI in `kamailio.cfg`
2. Check LiveKit SIP trunk allows your SBC IP
3. Test SIP connectivity to LiveKit:
   ```bash
   nc -zvu your-project.sip.livekit.cloud 5060
   ```

---

## Appendix B: Files Reference

The `sbc-proxy/` folder contains all necessary files for the self-hosted SBC:

```
sbc-proxy/
├── Dockerfile          # Docker image for Kamailio
├── docker-compose.yml  # Container orchestration
├── kamailio.cfg        # Main Kamailio configuration
├── tls.cfg             # TLS settings for Kamailio
├── setup-tls.sh        # Helper script for TLS setup
├── deploy.sh           # GCP deployment automation
├── env.example         # Environment variables template
├── README.md           # Quick start guide
└── tls/                # TLS certificates (created during setup)
    ├── server.pem
    ├── server.key
    └── ca.pem
```
