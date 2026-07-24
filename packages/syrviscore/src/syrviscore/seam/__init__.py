"""The management seam: SyrvisCore's enumerated remote-verb registry + generators.

This package is the platform's single source of truth for every management verb
a seam client may invoke on the NAS:

- ``registry``: the enumerated ``Command`` list (argv shapes, slots, privilege
  and destructiveness classification). The MCP server builds its ssh argv from
  it; the generators below derive the enforcement artifacts from it.
- ``gen``: renders the operator sudoers policy, the forced-command SSH shim,
  and the NAS provisioning script from the registry, so the enforcement
  boundary and the runtime argv can never drift (G18).

Clients (the MCP server, deployment tooling, ``syrvisctl``'s seam sync) are
generated consumers of this registry — verbs are added HERE, never forked in a
client. Import-light and Python 3.8-clean: this runs on the NAS (DSM 3.8.12)
as well as operator machines.
"""
