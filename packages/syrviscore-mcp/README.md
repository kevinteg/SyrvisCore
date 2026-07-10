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

## Provision the NAS (one-time, needs root)

The MCP cannot bootstrap its own access — a human installs the root-owned
operator account, sudoers policy, forced-command shim, and SSH key on the NAS.
This is fully scripted: you generate a **self-contained provisioning script on
your Mac**, copy that one file to the NAS, and run it with `sudo`. It is
idempotent, installs the sudoers policy **atomically** (a torn write can never
break `sudo`), and **records the original of every system file it touches**
first so it can revert exactly.

### Why the volume matters

`sudo` matches commands by their **absolute path**. The sudoers policy and shim
therefore contain the real paths to `syrvis`/`syrvisctl` on your NAS — by
default a `/volume1` install (`/volume1/syrviscore/bin/syrvis`,
`/var/packages/syrviscore/target/venv/bin/syrvisctl`). If the SPK installed
SyrvisCore on a **different volume** (check with `syrvisctl info` — e.g.
`/volume4/syrviscore`), you must generate with `--home /volume4/syrviscore` so
the policy matches the real command paths. Otherwise sudo denies everything.

### Step 1 — generate the provisioning script (on your Mac)

The generator needs only the Python standard library, so you can run it **without
installing anything** — straight from the repo with any Python 3.10+. From the
repo root:

```bash
# make an operator keypair (once), then generate YOUR script.
# --home is your SYRVIS_HOME (find it with `syrvisctl info`; default /volume1/syrviscore);
# --pubkey is the operator's public key; --from restricts which network may use that key.
ssh-keygen -t ed25519 -f ~/.ssh/syrvis_mcp_ed25519 -C syrvis-mcp

python3 packages/syrviscore-mcp/src/syrviscore_mcp/deploy/gen.py provision \
    --home /volume1/syrviscore \
    --pubkey ~/.ssh/syrvis_mcp_ed25519.pub \
    --from 192.168.8.0/24 \
    > /tmp/manual_mcp_account_provision.sh
```

(If you've installed the package into a venv — see the top of this README — the
equivalent is `python -m syrviscore_mcp.deploy.gen provision …`. Installing the
package is only required to *run the MCP server*, not to generate this script.)

Read the generated script — it is plain, auditable POSIX sh — then copy it over:

```bash
scp -O /tmp/manual_mcp_account_provision.sh cerebrate@192.168.8.3:/tmp/
```

### Step 2 — run it on the NAS (as root)

```bash
ssh cerebrate@192.168.8.3
sudo sh /tmp/manual_mcp_account_provision.sh --dry-run   # preview every action
sudo sh /tmp/manual_mcp_account_provision.sh             # apply
```

It creates the `syrvis-operator` user with the correct DSM `synouser` syntax
(`username password "full name" expired mail AppPrivilege` — SSH-key-only, a
random unused password), then gives it a real login shell. **DSM creates every
account with `/sbin/nologin`, which cannot run the forced-command shim** — sshd
executes the shim *through* the login shell, so a `nologin` operator makes every
key login fail with a misleading password prompt. The script sets the operator's
shell to `/bin/sh` **surgically** (rewriting only that one field of its
`/etc/passwd` line, atomically via rename — never a full-file overwrite). It
then ensures the `docker` group exists and adds the operator to it via
`--memberadd` (which does **not** replace existing members), installs the
sudoers policy (staged under a dotted name `sudo` ignores, then renamed into
place — atomic; `visudo`-validated too if your DSM has it), the shim, and the
operator key. The key install is **additive** — it preserves any other keys on
the account (e.g. a break-glass admin key) and just replaces its own line.

Before changing any system file it records the **true pre-install state once**
(under `/var/log/syrviscore-mcp-provision/original/`) and writes a
`/var/log/syrviscore-mcp-provision/rollback.sh` that reverts *exactly* —
restoring a file that existed, removing one the script created, or putting the
operator's login shell back (again field-surgically, not a passwd overwrite). To undo
everything: `sudo sh /var/log/syrviscore-mcp-provision/rollback.sh`. If
`synouser`/`synogroup` aren't available on your DSM version, the script tells
you to create the user / add the group via Control Panel and re-run. The
operator needs a home directory for its SSH key, so **DSM's user-home service
must be on** (Control Panel > User & Group > Advanced > Enable user home
service) — the script says so if it can't resolve the home.

### Step 3 — pin the host key + verify (on your Mac)

```bash
ssh-keyscan -H 192.168.8.3 >> ~/.config/syrviscore-mcp/known_hosts   # then confirm the fingerprint
ssh syrvis-nas 'id'                # rejected by the shim
ssh syrvis-nas 'sudo -n /bin/sh'   # denied by the sudoers policy
ssh syrvis-nas 'sudo -l'           # lists ONLY the enumerated commands
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
