#!/bin/bash
# deactivate.sh - Exit DSM 7.0 simulation environment
#
# Usage: source tests/dsm-sim/deactivate.sh

# Check if simulation is active
if [ -z "$DSM_SIM_ACTIVE" ]; then
    echo "DSM simulation is not active"
    return 0 2>/dev/null || exit 0
fi

# Restore original PATH
if [ -n "$_DSM_ORIG_PATH" ]; then
    export PATH="$_DSM_ORIG_PATH"
fi

# Restore original PS1
if [ -n "$_DSM_ORIG_PS1" ]; then
    export PS1="$_DSM_ORIG_PS1"
fi

# Restore or unset SYNOPKG_PKGDEST
if [ -n "$_DSM_ORIG_SYNOPKG_PKGDEST" ]; then
    export SYNOPKG_PKGDEST="$_DSM_ORIG_SYNOPKG_PKGDEST"
else
    unset SYNOPKG_PKGDEST
fi

# Restore or unset SYRVIS_HOME
if [ -n "$_DSM_ORIG_SYRVIS_HOME" ]; then
    export SYRVIS_HOME="$_DSM_ORIG_SYRVIS_HOME"
else
    unset SYRVIS_HOME
fi

# Clean up simulation variables
unset DSM_SIM_ACTIVE
unset DSM_SIM_ROOT
unset DSM_SIM_STATE
unset DSM_SIM_LOGS
unset PACKAGE_NAME

# Clean up wizard variables
unset pkgwizard_volume
unset pkgwizard_network_interface
unset pkgwizard_network_subnet
unset pkgwizard_gateway_ip
unset pkgwizard_traefik_ip
unset pkgwizard_domain
unset pkgwizard_acme_email
unset pkgwizard_cloudflare_token

# Clean up saved original values
unset _DSM_ORIG_PATH
unset _DSM_ORIG_SYNOPKG_PKGDEST
unset _DSM_ORIG_SYRVIS_HOME
unset _DSM_ORIG_PS1

echo ""
echo "DSM 7.0 simulation DEACTIVATED"
echo ""
