#!/usr/bin/env python3
"""
Create LiveKit SIP Inbound Trunk for Teams Direct Routing via Kamailio SBC.

Run once to set up the trunk:
    python create_livekit_trunk.py

Environment variables required:
    LIVEKIT_URL
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET

Note: No dispatch rule is created - the existing global dispatch rule
(with empty trunk_ids) applies to all trunks including Teams.
"""

import asyncio
import os
from dotenv import load_dotenv

# Load env from local .env file
load_dotenv()

from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
    ListSIPInboundTrunkRequest,
    ListSIPDispatchRuleRequest,
)

# Your Kamailio SBC public IP
SBC_IP = "34.121.162.2"
TRUNK_NAME = "Teams-Direct-Routing"


async def get_livekit_api():
    return LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )


async def create_teams_trunk():
    """Create the SIP inbound trunk for Teams calls via SBC."""
    lkapi = await get_livekit_api()

    # First, check if trunk already exists
    existing = await lkapi.sip.list_sip_inbound_trunk(ListSIPInboundTrunkRequest())
    for trunk in existing.items:
        if trunk.name == TRUNK_NAME:
            print(f"✅ Trunk '{TRUNK_NAME}' already exists: {trunk.sip_trunk_id}")
            await lkapi.aclose()
            return trunk.sip_trunk_id

    # Create new trunk
    sip_trunk = SIPInboundTrunkInfo(
        name=TRUNK_NAME,
        # No specific numbers - accept any (Teams sends the dialed number)
        numbers=[],
        # CRITICAL: Only allow calls from your SBC IP
        allowed_addresses=[SBC_IP],
        # Enable noise cancellation
        krisp_enabled=True,
    )

    request = CreateSIPInboundTrunkRequest(trunk=sip_trunk)
    result = await lkapi.sip.create_sip_inbound_trunk(request)

    print(f"✅ Created SIP trunk: {result.sip_trunk_id}")
    print(f"   Name: {TRUNK_NAME}")
    print(f"   Allowed IPs: {SBC_IP}")

    await lkapi.aclose()
    return result.sip_trunk_id


async def list_trunks():
    """List all existing trunks for debugging."""
    lkapi = await get_livekit_api()

    trunks = await lkapi.sip.list_sip_inbound_trunk(ListSIPInboundTrunkRequest())
    print("\n📋 Existing SIP Inbound Trunks:")
    for trunk in trunks.items:
        print(f"   - {trunk.name}: {trunk.sip_trunk_id}")
        print(f"     Numbers: {trunk.numbers}")
        print(f"     Allowed IPs: {trunk.allowed_addresses}")
        print()

    await lkapi.aclose()


async def list_dispatch_rules():
    """List all existing dispatch rules for debugging."""
    lkapi = await get_livekit_api()

    rules = await lkapi.sip.list_sip_dispatch_rule(ListSIPDispatchRuleRequest())
    print("\n📋 Existing SIP Dispatch Rules:")
    for rule in rules.items:
        print(f"   - {rule.sip_dispatch_rule_id}")
        print(f"     Trunk IDs: {list(rule.trunk_ids)}")
        print(f"     Metadata: {rule.metadata}")
        print(f"     Rule: {rule.rule}")
        print()

    await lkapi.aclose()


async def main():
    print("🔧 Setting up Teams Direct Routing SIP Trunk...\n")
    
    # Create trunk (dispatch rule not needed - existing global rule applies)
    await create_teams_trunk()
    
    # List everything for verification
    await list_trunks()
    await list_dispatch_rules()
    
    print("\n✅ Setup complete!")
    print("\nNext steps:")
    print("1. Deploy the updated kamailio.cfg to your SBC")
    print("2. Ensure your adapters endpoint /teams/call is deployed")
    print("3. Test by calling the Auto Attendant in Teams")


if __name__ == "__main__":
    asyncio.run(main())
