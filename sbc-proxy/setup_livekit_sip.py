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
    CreateSIPDispatchRuleRequest,
    SIPDispatchRule,
    SIPDispatchRuleDirect,
    RoomConfiguration,
    RoomAgentDispatch,
)
from livekit.protocol.sip import DeleteSIPDispatchRuleRequest

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
    existing = await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())
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
    result = await lkapi.sip.create_inbound_trunk(request)

    print(f"✅ Created SIP trunk: {result.sip_trunk_id}")
    print(f"   Name: {TRUNK_NAME}")
    print(f"   Allowed IPs: {SBC_IP}")

    await lkapi.aclose()
    return result.sip_trunk_id


async def create_teams_dispatch_rule(trunk_id: str, agent_name: str, room_name: str):
    """Create a dispatch rule that auto-dispatches an agent to answer calls.

    This is needed because LiveKit SIP calls are only answered (200 OK)
    when an agent is dispatched and joins the room.
    """
    lkapi = await get_livekit_api()

    # Delete existing Teams dispatch rules for this trunk
    existing = await lkapi.sip.list_dispatch_rule(ListSIPDispatchRuleRequest())
    for rule in existing.items:
        if trunk_id in rule.trunk_ids:
            print(f"🗑️  Deleting existing dispatch rule: {rule.sip_dispatch_rule_id}")
            await lkapi.sip.delete_dispatch_rule(
                DeleteSIPDispatchRuleRequest(
                    sip_dispatch_rule_id=rule.sip_dispatch_rule_id
                )
            )

    # Create dispatch rule with agent auto-dispatch
    # Using a hardcoded room name (same as agent name) for reliable matching
    request = CreateSIPDispatchRuleRequest(
        trunk_ids=[trunk_id],
        rule=SIPDispatchRule(
            # Use dispatchRuleDirect with a fixed room name
            dispatch_rule_direct=SIPDispatchRuleDirect(
                room_name=room_name,  # Hardcoded room name matching agent
                pin="",
            ),
        ),
        # Configure agent auto-dispatch - THIS IS KEY for auto-answering
        room_config=RoomConfiguration(
            agents=[
                RoomAgentDispatch(
                    agent_name=agent_name,
                )
            ]
        ),
    )

    result = await lkapi.sip.create_dispatch_rule(request)
    print(f"✅ Created dispatch rule: {result.sip_dispatch_rule_id}")
    print(f"   Trunk ID: {trunk_id}")
    print(f"   Agent: {agent_name}")
    print(f"   Room: {room_name}")

    await lkapi.aclose()
    return result.sip_dispatch_rule_id


async def list_trunks():
    """List all existing trunks for debugging."""
    lkapi = await get_livekit_api()

    trunks = await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())
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

    rules = await lkapi.sip.list_dispatch_rule(ListSIPDispatchRuleRequest())
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

    # Agent and room name must match what's registered in your agent worker
    # Using the same value for both ensures reliable matching
    AGENT_NAME = "unity_+19999999999"
    ROOM_NAME = "unity_+19999999999"  # Same as agent name for testing

    # Create trunk
    trunk_id = await create_teams_trunk()

    # Create dispatch rule with agent auto-dispatch
    # This is KEY - without this, calls won't be auto-answered
    await create_teams_dispatch_rule(trunk_id, AGENT_NAME, ROOM_NAME)

    # List everything for verification
    await list_trunks()
    await list_dispatch_rules()

    print("\n✅ Setup complete!")
    print("\nNext steps:")
    print("1. Start your agent worker: uv run call.py dev")
    print("2. Call the Auto Attendant in Teams")
    print("3. The call should auto-connect to your agent")


if __name__ == "__main__":
    asyncio.run(main())
