# syrviscore-mcp

An MCP server that exposes SyrvisCore NAS operations as tools to a Claude
session (e.g. from the `home-tech` operator repo). It runs **on the operator's
Mac**, not on the NAS, and drives `syrvis`/`syrvisctl` over SSH.

Design: `docs/mcp-design.md`. This README is the operator/NAS setup guide.

## Security model (why it's shaped this way)

- The server **never elevates itself**. Privileged subcommands run under
  `sudo -n` on the NAS, gated by an **enumerated NOPASSWD sudoers list** for a
  dedicated operator user — never blanket root.
- **No shell string is ever built.** Commands are argv lists of validated
  tokens; every user value passes strict validation before it can reach SSH.
- A **forced-command shim** on the NAS independently re-validates every request
  (`$SSH_ORIGINAL_COMMAND`), so a compromised client still can't run arbitrary
  commands.
- **Destructive tools require a confirmation token** (a two-call handshake that
  binds the exact args + current NAS state).
- `--purge`, `restore`, `reset`, `clean`, `setup`, and `install --wheel` are
  **intentionally not exposed** — those stay human-over-SSH operations.

## Install (operator Mac)

```bash
python3.12 -m venv ~/.venvs/syrviscore-mcp
~/.venvs/syrviscore-mcp/bin/pip install -e packages/syrviscore-mcp
```

Config lives at `~/.config/syrviscore-mcp/config.toml` (see
`docs/mcp-design.md` §4 for the full schema) plus a dedicated
`~/.config/syrviscore-mcp/ssh_config`. Set the HMAC token secret:

```bash
export SYRVISCORE_MCP_TOKEN_SECRET="$(openssl rand -hex 32)"
```

## Provision the NAS (one-time, needs root — do this yourself)

The MCP cannot bootstrap its own access; a human installs these root-owned
artifacts. Regenerate them first if your NAS uses a volume other than
`/volume1` (edit the paths in `src/syrviscore_mcp/deploy/gen.py`, or override):

```bash
# from packages/syrviscore-mcp
python -m syrviscore_mcp.deploy.gen sudoers > /tmp/syrviscore-mcp.sudoers
python -m syrviscore_mcp.deploy.gen shim    > /tmp/syrvis-mcp-shim
```

1. **Create a dedicated operator user** with docker access (not `admin`, not
   `cerebrate`, not `root`):
   ```
   sudo synouser --add syrvis-operator ... ; sudo synogroup --member docker syrvis-operator
   ```
2. **Install the sudoers policy** (validate before installing):
   ```
   sudo visudo -cf /tmp/syrviscore-mcp.sudoers
   sudo install -m 0440 -o root -g root /tmp/syrviscore-mcp.sudoers /etc/sudoers.d/syrviscore-mcp
   ```
3. **Install the forced-command shim**:
   ```
   sudo install -m 0755 -o root -g root /tmp/syrvis-mcp-shim /usr/local/bin/syrvis-mcp-shim
   ```
4. **Install the operator SSH key** with a forced command + source restriction
   in `~syrvis-operator/.ssh/authorized_keys` (0600):
   ```
   restrict,command="/usr/local/bin/syrvis-mcp-shim",from="192.168.8.0/24" ssh-ed25519 AAAA... syrvis-mcp
   ```
5. **Pin the host key** into `~/.config/syrviscore-mcp/known_hosts` on the Mac.

Then verify the boundary from the Mac (these MUST behave as noted):

```bash
ssh syrvis-nas 'id'                                   # rejected by the shim
ssh syrvis-nas 'sudo -n /bin/sh'                      # denied by sudoers
ssh syrvis-nas 'sudo -n .../syrvis service remove -- x --purge -y'   # denied (no purge)
ssh syrvis-nas 'sudo -l'                              # matches the enumerated allowlist exactly
```

## Register with a Claude session

`.mcp.json` (e.g. in `home-tech`):

```json
{
  "mcpServers": {
    "syrviscore": {
      "command": "~/.venvs/syrviscore-mcp/bin/python",
      "args": ["-m", "syrviscore_mcp"],
      "env": { "SYRVISCORE_MCP_TOKEN_SECRET": "..." }
    }
  }
}
```

## Tools

23 tools in three tiers — read-only (`status`, `verify`, `service_list`,
`logs`, `versions_list`, `check_updates`, `info`, `backup_list`,
`cleanup_preview`), privileged non-destructive (`start`, `stop`, `restart`,
`verify_fix`, `service_start/stop/update/add`, `install`), and privileged +
destructive with a confirmation handshake (`activate`, `rollback`, `uninstall`,
`cleanup`, `service_remove`). See `docs/mcp-design.md` §2.

## Development

```bash
~/.venvs/syrviscore-mcp/bin/pytest packages/syrviscore-mcp/tests
```

The suite is fully offline (no NAS, no fastmcp needed for the logic tests).
`deploy/sudoers.d/syrviscore-mcp` and `deploy/ssh/syrvis-mcp-shim` are generated
artifacts; a drift test fails if they don't match `deploy/gen.py`.
