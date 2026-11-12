# Telephony: give the receptionist a phone number

Browser callers reach the agent over WebRTC. This directory connects a real phone number to the
same agent over SIP, so people can dial Lakeside Family Clinic from any phone.

```
caller's phone ──PSTN──▶ Twilio number ──Elastic SIP trunk──▶ LiveKit SIP
                                                                 │  inbound trunk (accepts your number)
                                                                 ▼  dispatch rule
                                               room call-_+15557654321_<random>
                                               + agent "receptionist" dispatched with
                                                 {"mode": "realtime", "language": "auto"}
```

| File | What it is |
|---|---|
| `inbound-trunk.json` | `lk sip inbound create` request: which number(s) LiveKit accepts calls for |
| `dispatch-rule.json` | `lk sip dispatch create` request: one room per call, dispatching the `receptionist` agent |
| `setup_sip.py` | Creates or updates both through the LiveKit server API; safe to re-run |

Phone callers get the same agent and dispatch metadata as browser callers, with one difference:
when they ask for a person, the agent transfers the call to `HUMAN_TRANSFER_NUMBER` instead of filing a web escalation.

## Before you start

- A LiveKit Cloud project (SIP is built in) or a self-hosted LiveKit server with
  [livekit-sip](https://github.com/livekit/sip).
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` in your environment or `.env`.
- The agent worker running and registered as `receptionist`:
  `python -m receptionist.agent dev` (or `start` in production).
- Optional: the LiveKit CLI (`lk`), signed in with `lk cloud auth` or `lk project add`.
- A Twilio account with a little credit (or Telnyx, below).

## 1. Find your LiveKit SIP URI

LiveKit Cloud → **Settings → Project → SIP URI**. It looks like
`sip:abc123xyz.sip.livekit.cloud`. Use this host exactly. It is based on your project ID and is
**not** the `wss://…livekit.cloud` hostname from `LIVEKIT_URL`; mixing them up is the most common
reason calls never arrive.

Self-hosted: use the public address of your `livekit-sip` service (UDP/TCP 5060, TLS 5061).

## 2. Buy a number on Twilio

Console → **Phone Numbers → Manage → Buy a number**, with the *Voice* capability. Note the number in
E.164 form (`+15551234567`) and its SID (`PN…`).

## 3. Create an Elastic SIP trunk that points at LiveKit

In the console: **Elastic SIP Trunking → Manage → Trunks → Create new SIP Trunk**, then:

1. **Origination → Add new Origination URI**:
   `sip:<your-project-id>.sip.livekit.cloud;transport=tcp`, priority 1, weight 1, enabled.
   (Use `;transport=tls` if you've turned on secure trunking in LiveKit.)
2. **Numbers → Add an existing number** → the number from step 2.

Or with the [Twilio CLI](https://www.twilio.com/docs/twilio-cli):

```bash
twilio api trunking v1 trunks create \
  --friendly-name "lakeside-livekit" \
  --domain-name "lakeside-livekit.pstn.twilio.com"      # must end in pstn.twilio.com

twilio api trunking v1 trunks origination-urls create \
  --trunk-sid TKxxxxxxxx \
  --friendly-name "LiveKit SIP" \
  --sip-url "sip:<your-project-id>.sip.livekit.cloud;transport=tcp" \
  --weight 1 --priority 1 --enabled

twilio api trunking v1 trunks phone-numbers create \
  --trunk-sid TKxxxxxxxx \
  --phone-number-sid PNxxxxxxxx
```

Twilio's Elastic SIP trunking doesn't send digest credentials on calls it originates, so don't set
`auth_username`/`auth_password` on the LiveKit inbound trunk. The trunk only accepts calls to the
numbers you list. To go further, restrict signalling to Twilio's published IP ranges with
`--allowed-address` (see Twilio's "IP addresses for Elastic SIP Trunking" page for the current list).

## 4. Create the LiveKit inbound trunk and dispatch rule

### Option A: the script (idempotent)

```bash
source .venv/bin/activate
python telephony/setup_sip.py --number +15551234567 --dry-run   # show what would change
python telephony/setup_sip.py --number +15551234567             # create or update
```

It finds the trunk and rule by name (`lakeside-inbound`, `lakeside-receptionist`) and updates them
in place, keeping fields it doesn't manage (auth, headers, media settings) as they are. Run it
again whenever you change a flag:

```bash
# Spanish-first pipeline agent, answering only your own phone while you test
python telephony/setup_sip.py --number +15551234567 \
  --mode pipeline --language es \
  --allowed-caller +15557654321
```

`--print-json` prints the equivalent `lk` request bodies without touching the network. Run
`python telephony/setup_sip.py --help` for every option.

### Option B: the `lk` CLI with the JSON files

```bash
# 1. Put your number in inbound-trunk.json ("+1XXXXXXXXXX"), then:
lk sip inbound create telephony/inbound-trunk.json        # prints the trunk ID, ST_…

# 2. Put that ID in dispatch-rule.json ("ST_REPLACE_WITH_TRUNK_ID"), or delete the
#    "trunk_ids" line to apply the rule to every inbound trunk, then:
lk sip dispatch create telephony/dispatch-rule.json       # prints the rule ID, SDR_…

# 3. Check:
lk sip inbound list
lk sip dispatch list
```

To change them later, `lk sip inbound update --id ST_… file.json` and
`lk sip dispatch update --id SDR_… file.json` take the *bare* object (the contents of `"trunk"` /
`"dispatch_rule"`, without that wrapper key) and replace the whole resource.

### What the rule does

- `dispatch_rule_individual` with `room_prefix: "call-"` puts every call in a new room named
  `call-_<caller number>_<random>`, mirroring the `call-…` rooms the web token server creates.
- `room_config.agents` dispatches the worker named by `AGENT_NAME` (default `receptionist`) with
  the metadata JSON as job metadata, which is
  the same contract as browser calls. Change `mode` or `language` here to pick
  the architecture phone callers get.
- The caller joins as a SIP participant (identity `sip_+15557654321`, kind `SIP`) with attributes
  such as `sip.phoneNumber`, which the agent can use as a hint when verifying identity.
- `krisp_enabled` turns on LiveKit Cloud's noise cancellation for the phone leg.

## 5. Let the agent transfer callers to a person (SIP REFER)

When a phone caller asks for a human, the agent calls LiveKit's `TransferSIPParticipant` with
`transfer_to = HUMAN_TRANSFER_NUMBER`. LiveKit sends a SIP **REFER** to your carrier, the carrier
connects the caller to that number, and the caller leaves the LiveKit room (a *cold* transfer).

1. Set the destination in `.env`, as a phone number or a SIP URI:

   ```bash
   HUMAN_TRANSFER_NUMBER=+15550109999
   # or HUMAN_TRANSFER_NUMBER=tel:+15550109999
   # or HUMAN_TRANSFER_NUMBER=sip:frontdesk@pbx.example.com
   ```

   If the transfer fails, the agent tells the caller it didn't go through and stays on the call.
   For a *warm* transfer instead (the agent dials a staff member on an outbound trunk, briefs
   them, then bridges the caller), also set `HUMAN_TRANSFER_MODE=warm` and
   `LIVEKIT_SIP_OUTBOUND_TRUNK`.

2. Allow transfers on the Twilio trunk. Without this, REFER is rejected and the caller stays with
   the agent.
   - Console: your trunk → **General → Call Transfer (SIP REFER)** → enable, and turn on
     **Enable PSTN Transfer** so transfers to ordinary phone numbers work. Choose the caller ID
     the human sees: *Transferee* (the patient's number) or *Transferor* (your Twilio number).
   - CLI:

     ```bash
     twilio api trunking v1 trunks update --sid TKxxxxxxxx \
       --transfer-mode enable-all \
       --transfer-caller-id from-transferee
     ```

   Twilio bills the transferred leg as a new outbound call.

3. Try it without the agent, from another terminal during a live call:

   ```bash
   lk room list                                       # find the call-_… room
   lk room participants list call-_+15557654321_AbCd  # find the sip_… identity
   lk sip participant transfer --room call-_+15557654321_AbCd \
     --identity sip_+15557654321 --to tel:+15550109999 --play-dialtone
   ```

## Using Telnyx instead

The LiveKit side (step 4) is identical. On Telnyx:

1. Buy a number (**Numbers → Search & Buy**, voice-enabled).
2. Create a **SIP Connection** of type **FQDN** and add your LiveKit SIP host
   (`<your-project-id>.sip.livekit.cloud`, TCP 5060, or TLS 5061) as the FQDN.
3. In the connection's inbound settings, send the destination number as **+E.164** so it matches
   the `numbers` on the LiveKit trunk exactly.
4. Assign the number to the connection.
5. For transfers, make sure SIP REFER is allowed on the connection and that it has an
   **Outbound Voice Profile**, because the transferred leg is an outbound call. If every transfer
   fails with `603 Decline`, ask Telnyx support to enable REFER for your account.

## Testing tips

- **Start the agent first.** With no worker registered under `AGENT_NAME`, the room is created but
  nobody answers. Watch the worker log for the job, and `lk room list` for the `call-_…` room.
- **LiveKit Cloud → Telephony → Calls** shows each SIP call with its status code. It's the fastest
  way to tell "never reached LiveKit" from "reached LiveKit but no rule matched".
- **Calls never arrive / stuck at `100 Processing`:** the origination URI uses the wrong host (see
  step 1), or the dialled number isn't listed on the inbound trunk in the same format (`+1…`).
- **Call connects, then silence:** the agent worker isn't running, or it registered under a
  different `AGENT_NAME` than the rule dispatches. Compare with `dispatch-rule.json`.
- **"No dispatch rule" / rejected:** a rule's `trunk_ids` must include your trunk's ID, or be empty.
  Two rules matching the same trunk and number conflict; `setup_sip.py` warns about that.
- **Transfer fails (`403`/`603`) and the caller stays with the agent:** REFER or PSTN transfer
  isn't enabled on the carrier trunk (step 5).
- **Keep a public demo number private** while you iterate: `--allowed-caller <your phone>` makes
  LiveKit reject every other caller ID.
- **Choosing the model per line:** buy two numbers, create two inbound trunks and two rules with
  different metadata (for example one `realtime`, one `pipeline`), and compare them side by side.
