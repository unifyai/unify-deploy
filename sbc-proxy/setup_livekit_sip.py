#!/usr/bin/env python3
"""
Create LiveKit SIP Trunks for Teams Direct Routing via Kamailio SBC.

This script sets up:
1. INBOUND trunk - for receiving calls from Teams users
2. OUTBOUND trunk - for making calls TO Teams users

Run once to set up the trunks:
    python setup_livekit_sip.py

Environment variables required:
    LIVEKIT_URL
    LIVEKIT_API_KEY
    LIVEKIT_API_SECRET
"""

import asyncio
import os
from dotenv import load_dotenv

# Load env from local .env file
load_dotenv()

from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    SIPOutboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
    CreateSIPOutboundTrunkRequest,
    ListSIPInboundTrunkRequest,
    ListSIPOutboundTrunkRequest,
    ListSIPDispatchRuleRequest,
    CreateSIPDispatchRuleRequest,
    SIPDispatchRule,
    SIPDispatchRuleDirect,
    RoomConfiguration,
    RoomAgentDispatch,
)
from livekit.protocol.sip import DeleteSIPDispatchRuleRequest

# Your Kamailio SBC configuration
SBC_IP = "34.121.162.2"
SBC_DOMAIN = "sbc.unify.ai"
SBC_PORT = 5060  # UDP port for SBC (LiveKit → SBC)

# Trunk names
INBOUND_TRUNK_NAME = "Teams-Direct-Routing"
OUTBOUND_TRUNK_NAME = "Teams-Outbound"


async def get_livekit_api():
    return LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )


async def create_inbound_trunk():
    """Create the SIP inbound trunk for receiving Teams calls via SBC."""
    lkapi = await get_livekit_api()

    # First, check if trunk already exists
    existing = await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())
    for trunk in existing.items:
        if trunk.name == INBOUND_TRUNK_NAME:
            print(
                f"✅ Inbound trunk '{INBOUND_TRUNK_NAME}' already exists: {trunk.sip_trunk_id}"
            )
            await lkapi.aclose()
            return trunk.sip_trunk_id

    # Create new trunk
    sip_trunk = SIPInboundTrunkInfo(
        name=INBOUND_TRUNK_NAME,
        # No specific numbers - accept any (Teams sends the dialed number)
        numbers=[],
        # CRITICAL: Only allow calls from your SBC IP
        allowed_addresses=[SBC_IP],
        # Enable noise cancellation
        krisp_enabled=True,
    )

    request = CreateSIPInboundTrunkRequest(trunk=sip_trunk)
    result = await lkapi.sip.create_inbound_trunk(request)

    print(f"✅ Created inbound SIP trunk: {result.sip_trunk_id}")
    print(f"   Name: {INBOUND_TRUNK_NAME}")
    print(f"   Allowed IPs: {SBC_IP}")

    await lkapi.aclose()
    return result.sip_trunk_id


async def create_outbound_trunk():
    """Create the SIP outbound trunk for making calls TO Teams users via SBC."""
    lkapi = await get_livekit_api()

    # First, check if trunk already exists
    existing = await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
    for trunk in existing.items:
        if trunk.name == OUTBOUND_TRUNK_NAME:
            print(
                f"✅ Outbound trunk '{OUTBOUND_TRUNK_NAME}' already exists: {trunk.sip_trunk_id}"
            )
            await lkapi.aclose()
            return trunk.sip_trunk_id

    # Create new outbound trunk
    # LiveKit will send SIP INVITE to this address for outbound calls
    sip_trunk = SIPOutboundTrunkInfo(
        name=OUTBOUND_TRUNK_NAME,
        # SBC address - LiveKit sends outbound calls here
        address=f"{SBC_DOMAIN}:{SBC_PORT}",
        # Transport protocol
        transport=1,  # 1 = UDP, 2 = TCP (SBC listens on UDP 5060)
        # Numbers - the caller ID(s) to use for outbound calls
        # This should be your Teams Resource Account phone number
        numbers=["+19999999999"],
    )

    request = CreateSIPOutboundTrunkRequest(trunk=sip_trunk)
    result = await lkapi.sip.create_outbound_trunk(request)

    print(f"✅ Created outbound SIP trunk: {result.sip_trunk_id}")
    print(f"   Name: {OUTBOUND_TRUNK_NAME}")
    print(f"   Address: {SBC_DOMAIN}:{SBC_PORT}")

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

    # List inbound trunks
    inbound = await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())
    print("\n📋 Existing SIP Inbound Trunks:")
    for trunk in inbound.items:
        print(f"   - {trunk.name}: {trunk.sip_trunk_id}")
        print(f"     Numbers: {trunk.numbers}")
        print(f"     Allowed IPs: {trunk.allowed_addresses}")
        print()

    # List outbound trunks
    outbound = await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
    print("📋 Existing SIP Outbound Trunks:")
    for trunk in outbound.items:
        print(f"   - {trunk.name}: {trunk.sip_trunk_id}")
        print(f"     Address: {trunk.address}")
        print(f"     Transport: {trunk.transport}")
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


async def call_teams_user(phone_number: str, room_name: str):
    """
    Make an outbound call to a Teams user (or any phone number via Teams).

    Args:
        phone_number: The phone number to call (e.g., "+14155551234")
        room_name: The LiveKit room name where the call will be connected

    Returns:
        The SIP participant info

    Example:
        await call_teams_user("+14155551234", "outbound-call-123")
    """
    from livekit.api import CreateSIPParticipantRequest

    lkapi = await get_livekit_api()

    # Get the outbound trunk ID
    outbound = await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
    outbound_trunk_id = None
    for trunk in outbound.items:
        if trunk.name == OUTBOUND_TRUNK_NAME:
            outbound_trunk_id = trunk.sip_trunk_id
            break

    if not outbound_trunk_id:
        raise ValueError(
            f"Outbound trunk '{OUTBOUND_TRUNK_NAME}' not found. Run setup first."
        )

    # Create a SIP participant that dials out
    # The call goes: LiveKit → SBC → Microsoft → Teams User
    result = await lkapi.sip.create_sip_participant(
        CreateSIPParticipantRequest(
            sip_trunk_id=outbound_trunk_id,
            # Format: sip:+phonenumber@sbc.domain
            # The SBC will route this to Microsoft
            sip_call_to=f"sip:{phone_number}@{SBC_DOMAIN}",
            room_name=room_name,
            participant_identity=f"call-{phone_number}",
            participant_name=f"Call to {phone_number}",
        )
    )

    print(f"📞 Initiated outbound call:")
    print(f"   To: {phone_number}")
    print(f"   Room: {room_name}")
    print(f"   Participant: {result.participant_identity}")

    await lkapi.aclose()
    return result


async def main():
    print("🔧 Setting up Teams Direct Routing SIP Trunks...\n")

    # Agent and room name must match what's registered in your agent worker
    # Using the same value for both ensures reliable matching
    AGENT_NAME = "unity_+19999999999"
    ROOM_NAME = "unity_+19999999999"  # Same as agent name for testing

    # Create inbound trunk (for receiving calls FROM Teams)
    # inbound_trunk_id = await create_inbound_trunk()

    # Create outbound trunk (for making calls TO Teams)
    outbound_trunk_id = await create_outbound_trunk()
    return

    # Create dispatch rule with agent auto-dispatch (for inbound calls)
    # This is KEY - without this, inbound calls won't be auto-answered
    await create_teams_dispatch_rule(inbound_trunk_id, AGENT_NAME, ROOM_NAME)

    # List everything for verification
    await list_trunks()
    await list_dispatch_rules()

    print("\n✅ Setup complete!")
    print("\n" + "=" * 60)
    print("INBOUND CALLS (Teams → Agent):")
    print("  1. Start your agent worker: uv run call.py dev")
    print("  2. Call the Auto Attendant in Teams")
    print("  3. The call should auto-connect to your agent")
    print()
    print("OUTBOUND CALLS (Agent → Teams):")
    print("  Use the call_teams_user() function:")
    print('    await call_teams_user("+14155551234", "my-room")')
    print()
    print(f"  Outbound trunk ID: {outbound_trunk_id}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
