# syrvis-homeassistant

A Home Assistant service for SyrvisCore.

## What is Home Assistant?

[Home Assistant](https://www.home-assistant.io/) is an open-source home automation platform. It can control and monitor smart home devices, create automations, and integrate with over 2000 services.

## Installation

```bash
syrvis service add https://github.com/yourusername/syrvis-homeassistant.git
```

## Access

Once installed, access Home Assistant at: `https://ha.yourdomain.com`

## Data Storage

Configuration is stored in `$SYRVIS_HOME/data/homeassistant/config/`

## Configuration

Edit `syrvis-service.yaml` to customize:
- `traefik.subdomain` - Change the subdomain (default: ha)
- `environment.TZ` - Set your timezone

## Notes

- First startup takes several minutes as Home Assistant initializes
- For Zigbee/Z-Wave support, you may need to add device passthrough
- Consider using `host` network mode for mDNS discovery (requires config change)
