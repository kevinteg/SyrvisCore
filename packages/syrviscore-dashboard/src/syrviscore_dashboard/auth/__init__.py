"""Pluggable authentication for the dashboard.

A request is authenticated if it presents EITHER a valid Cloudflare Access JWT
(remote + local-via-hairpin) OR a valid local OIDC session cookie. ``none`` mode
is a LAN/dev bypass. ``DASHBOARD_AUTH_MODE`` selects which providers are active.
"""
