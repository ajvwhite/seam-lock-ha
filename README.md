# Seam Smart Lock — Home Assistant Integration

**Author:** ajvwhite
**Version:** 2.2.1

An integration to use Seam to access Smart Locks to expose more comprehensive information. This was initially created to fix limitations in direct manufacturer implementations.

## Features

- **Real-time updates** via Seam webhooks (1–3 second latency) with polling fallback
- **Who unlocked the door** — resolves access code IDs to names
- **Full event history** with timestamps, methods, and actor identification
- **State persistence** across Home Assistant restarts via `RestoreEntity`
- **Webhook signature verification** (HMAC-SHA256 with replay protection)
- **Diagnostics support** (Settings → Devices → Download diagnostics)
- **Privacy-first** — no PINs, secrets, or external URLs exposed in entity state

## Entities Created

| Entity | Type | Description |
|--------|------|-------------|
| Lock | `lock` | Lock/unlock control with state |
| Battery | `sensor` | Battery percentage (diagnostic) |
| Last Unlocked By | `sensor` | Who performed the last unlock |
| Last Unlock Time | `sensor` | Timestamp of last unlock |
| Last Unlock Method | `sensor` | How the lock was unlocked |
| Unlocks Today | `sensor` | Daily unlock counter |
| Registered Access Codes | `sensor` | Count of Seam-managed codes (diagnostic) |
| Lock Event Log | `sensor` | Full event history in attributes (diagnostic) |
| Connectivity | `binary_sensor` | Online/offline status (diagnostic) |
| Door | `binary_sensor` | Open/closed (if hardware supports it) |
| Webhook Active | `binary_sensor` | Whether webhook secret is configured (diagnostic) |

## Installation

### Manual

1. Copy the `custom_components/seam_lock/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration → Seam Smart Lock**

### HACS (Custom Repository)

1. In HACS, select **Integrations → ⋮ → Custom Repositories**
2. Add the repository URL and select category **Integration**
3. Install **Seam Smart Lock** and restart Home Assistant

## Setup

1. **Get a Seam API key** from [console.seam.co](https://console.seam.co)
2. **Link your Schlage account** in the Seam Console under Connected Accounts
3. **Add the integration** in HA — enter your API key and select the lock
4. The lock name from the Seam API will be used automatically. You can rename it later in Settings → Devices.

## Webhook Setup (Recommended)

Webhooks provide near-instant updates (1–3 seconds) instead of waiting for the next poll cycle.

### Prerequisites

Your HA instance must be reachable from the internet via HTTPS. Options include:
- [Nabu Casa](https://www.nabucasa.com/) (easiest)
- Cloudflare Tunnel
- DuckDNS + Let's Encrypt
- Tailscale Funnel

### Steps

1. After adding the integration, check your **HA notifications** (bell icon) for the webhook URL
2. Go to [Seam Console → Webhooks](https://console.seam.co) and add the URL
3. Select event types: `lock.locked`, `lock.unlocked`, `lock.access_denied`, `device.connected`, `device.disconnected`, `device.low_battery`
4. Copy the webhook secret (starts with `whsec_`)
5. In HA: **Settings → Devices & Services → Seam Smart Lock → Configure** — paste the secret
6. The setup notification will automatically dismiss once the secret is saved

When the webhook secret is configured, the polling interval relaxes to 5 minutes (background reconciliation only). Without webhooks, polling runs every 30 seconds.

## Integration Icon / Logo

Custom integrations cannot bundle their own icons for the HA frontend. To see the Seam Lock icon in your Devices & Services page, icons must be submitted to the [home-assistant/brands](https://github.com/home-assistant/brands) repository on GitHub under `custom_integrations/seam_lock/`. See the brands repo README for image specifications.

## Access Code Setup

For "who unlocked" tracking to work, access codes **must** be created via the Seam API (not the Schlage app):

```python
from seam import Seam

seam = Seam(api_key="your_key")
lock = seam.locks.list()[0]
seam.access_codes.create(
    device_id=lock.device_id,
    code="1234",
    name="Alex"
)
```

When code 1234 is used, the integration will show "Alex" as the person who unlocked.

## Security & Privacy

- **API key** stored encrypted in HA's config entry storage
- **Webhook secret** stored in config entry options; verified via HMAC-SHA256
- **Replay protection** rejects webhook payloads with timestamps older than 5 minutes
- **No PINs exposed** — access codes show display names only, never the actual digits
- **No external URL leak** — webhook URL is shown only in the setup notification, not in entity attributes
- **Diagnostics redaction** — API keys and webhook secrets are redacted in diagnostic downloads
- **Access code names only** — the `code_names` attribute on the access code count sensor lists names (e.g., "Alex"), never PINs

## Automation Examples

### Instant unlock notification

```yaml
automation:
  - alias: "Door Unlocked"
    trigger:
      - platform: state
        entity_id: sensor.pantry_door_last_unlocked_by
    action:
      - service: notify.mobile_app
        data:
          title: "Door Unlocked"
          message: "{{ trigger.to_state.state }} unlocked at {{ now().strftime('%H:%M') }}"
```

### Auto-lock after 5 minutes

```yaml
automation:
  - alias: "Auto Lock"
    trigger:
      - platform: state
        entity_id: lock.pantry_door
        to: "unlocked"
        for: "00:05:00"
    action:
      - service: lock.lock
        target:
          entity_id: lock.pantry_door
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Sensors not updating from webhooks | Ensure the webhook secret is saved in the integration options |
| "Last Unlocked By" shows "N/A" | Create access codes via Seam API, not the Schlage app |
| Webhook URL shows `http://` | Set up HTTPS — Seam requires it |
| Webhook URL shows `homeassistant.local` | Configure your external URL in HA: Settings → System → Network |
| Signature verification fails | Ensure the full `whsec_...` string is copied including the prefix |
| Icon not showing | Submit icons to the [home-assistant/brands](https://github.com/home-assistant/brands) repo |

## License

MIT
