# Synology DSM 7.x Installation Wizard Guide

## Overview
This document details the installation wizard configuration for SyrvisCore SPK package on Synology DSM 7.x.

## Directory Structure
```
spk/
├── INFO                           # Package metadata with wizard directive
└── WIZARD_UIFILES/
    └── install_uifile.json       # Wizard configuration (5 steps)
```

## INFO File Configuration

### Required Directive
```ini
install_wizard="yes"
```

This directive tells DSM to look for wizard configuration in `WIZARD_UIFILES/install_uifile.json`.

### Other Wizard-Related Directives (DSM 7.x)

Common alternatives if the standard directive doesn't work:
- `install_wizard="yes"` (standard, recommended)
- `installer_type="wizard"` (alternative)
- `install_wizard_uifile="install_uifile.json"` (explicit path)
- `wizard_uifile="install_uifile.json"` (shorter form)

**Current Configuration:** We use the standard `install_wizard="yes"` directive.

## Wizard File Structure

### Location
The wizard file must be located at:
```
WIZARD_UIFILES/install_uifile.json
```

This path is case-sensitive and must be exact.

### JSON Format
The wizard is an array of step objects:

```json
[
    {
        "step_title": "Step Title",
        "items": [
            {
                "type": "textfield",
                "desc": "Description shown above field",
                "subitems": [
                    {
                        "key": "pkgwizard_variable_name",
                        "desc": "Field Label",
                        "defaultValue": "default value",
                        "validator": {
                            "allowBlank": false,
                            "regex": {
                                "expr": "/^pattern$/",
                                "errorText": "Error message"
                            }
                        }
                    }
                ]
            }
        ]
    }
]
```

## SyrvisCore Wizard Steps

### Step 1: Basic Configuration
- **Purpose:** Select installation volume
- **Type:** `combobox` (API-driven dropdown)
- **Variable:** `pkgwizard_volume`
- **Default:** `/volume1`
- **API:** `SYNO.Core.Storage.Volume.list` (dynamically lists available volumes)

### Step 2: Network Configuration
- **Purpose:** Configure macvlan network for Traefik
- **Type:** `textfield` (multiple fields)
- **Variables:**
  - `pkgwizard_network_interface` (default: `ovs_eth0`)
  - `pkgwizard_network_subnet` (default: `192.168.0.0/24`)
  - `pkgwizard_gateway_ip` (default: `192.168.0.1`)
  - `pkgwizard_traefik_ip` (default: `192.168.0.4`)
- **Validation:** Regex for interface names, CIDR notation, IP addresses

### Step 3: Domain and SSL Configuration
- **Purpose:** Configure domain and Let's Encrypt email
- **Type:** `textfield`
- **Variables:**
  - `pkgwizard_domain` (required, validated domain format)
  - `pkgwizard_acme_email` (required, validated email format)

### Step 4: Optional Cloudflare Tunnel
- **Purpose:** Configure Cloudflare Tunnel token (optional)
- **Type:** `textfield`
- **Variable:** `pkgwizard_cloudflare_token`
- **Validation:** `allowBlank: true` (optional field)

### Step 5: Post-Installation Warning
- **Purpose:** Display important post-installation instructions
- **Type:** `textfield` (informational only)
- **Variable:** `pkgwizard_post_install_ack` (hidden field with default "yes")
- **HTML:** Simplified (no complex CSS styling)

## HTML Formatting in Wizard

### Supported Tags (DSM 7.x)
Based on DSM's wizard engine, the following HTML tags are generally safe:
- `<b>` and `<strong>` - Bold text
- `<i>` and `<em>` - Italic text
- `<br>` - Line breaks
- `<code>` - Inline code (basic styling)
- Basic text and line breaks

### Not Recommended
- Inline CSS (`style='...'` attributes) - May not be supported
- Complex CSS properties (`display:block`, `padding`, `background`, `border-radius`)
- `<div>`, `<span>` with styling
- Custom font families

### Best Practice
Use simple HTML tags without inline styling:
```html
<b>IMPORTANT:</b> After installation completes, you must run:<br><br><code>command here</code>
```

Instead of:
```html
<strong>IMPORTANT:</strong> ... <code style='display:block;padding:10px;...'>command</code>
```

## Variable Passing to Scripts

### How It Works
When the wizard completes, DSM sets environment variables in the installation scripts:

1. User enters value in wizard field with key `pkgwizard_domain`
2. DSM creates environment variable: `pkgwizard_domain="user-entered-value"`
3. Installation scripts (`postinst`, etc.) can access: `$pkgwizard_domain`

### Example (postinst script)
```bash
#!/bin/bash

# Wizard variables are available as environment variables
VOLUME="${pkgwizard_volume}"
NETWORK_INTERFACE="${pkgwizard_network_interface}"
DOMAIN="${pkgwizard_domain}"
ACME_EMAIL="${pkgwizard_acme_email}"

# Log what was received
log_info "Installation parameters from wizard:"
log_info "  Volume: ${VOLUME}"
log_info "  Network Interface: ${NETWORK_INTERFACE}"
log_info "  Domain: ${DOMAIN}"
log_info "  ACME Email: ${ACME_EMAIL}"
```

### Debugging Missing Variables
If variables show as `<not set>` in logs:

1. **Check wizard file syntax:**
   ```bash
   python3 -m json.tool spk/WIZARD_UIFILES/install_uifile.json
   ```

2. **Verify INFO file directive:**
   ```bash
   grep "install_wizard" spk/INFO
   ```

3. **Check variable names:**
   - Must start with `pkgwizard_`
   - Use consistent naming in wizard and scripts

4. **Verify SPK contents:**
   ```bash
   tar -tf dist/package.spk | grep WIZARD_UIFILES
   ```

5. **Test wizard displays:**
   - Install package via Package Center UI
   - Wizard should show 5 steps
   - All fields should be editable (except combobox)
   - Default values should be populated

## Validation

### Build-Time Validation
The build script now validates:

1. **Wizard file exists:**
   ```bash
   [ -f spk/WIZARD_UIFILES/install_uifile.json ]
   ```

2. **JSON syntax is valid:**
   ```bash
   python3 -m json.tool spk/WIZARD_UIFILES/install_uifile.json > /dev/null
   ```

3. **INFO has wizard directive:**
   ```bash
   grep -q '^install_wizard="yes"' spk/INFO
   ```

4. **Count wizard steps:**
   ```bash
   python3 -c "import json; print(len(json.load(open('spk/WIZARD_UIFILES/install_uifile.json'))))"
   ```

### Runtime Validation (in postinst)
```bash
# Check if wizard variables were passed
if [ -z "$pkgwizard_domain" ]; then
    log_error "Wizard variable 'pkgwizard_domain' not set"
    exit 1
fi
```

## Troubleshooting

### Wizard Doesn't Display
**Symptoms:** Installation proceeds without showing wizard

**Possible Causes:**
1. Missing `install_wizard="yes"` in INFO file
2. `WIZARD_UIFILES/` directory not in SPK package
3. Wrong path/filename (case-sensitive)
4. Invalid JSON syntax in wizard file

**Solution:**
```bash
# Verify INFO file
grep install_wizard spk/INFO

# Verify wizard file in SPK
tar -tf dist/package.spk | grep WIZARD_UIFILES

# Validate JSON
python3 -m json.tool spk/WIZARD_UIFILES/install_uifile.json
```

### Fields Not Editable
**Symptoms:** Wizard displays but fields are grayed out

**Possible Causes:**
1. `editable: false` set on textfield (only use for combobox)
2. Wrong field type

**Solution:**
- Remove `editable: false` from textfield items
- Use `type: "textfield"` for user input
- Use `type: "combobox"` only for dropdowns

### Variables Not Passed to Scripts
**Symptoms:** `$pkgwizard_variable` is empty in postinst

**Possible Causes:**
1. Variable key doesn't start with `pkgwizard_`
2. JSON syntax error in wizard file
3. Wizard didn't display (silent failure)

**Solution:**
- All keys must start with `pkgwizard_`
- Check DSM system logs: `/var/log/synopkg.log`
- Test wizard JSON with validation script

### HTML Not Rendering
**Symptoms:** HTML tags show as literal text or don't display

**Possible Causes:**
1. DSM wizard engine doesn't support complex HTML/CSS
2. Syntax errors in HTML

**Solution:**
- Use simple HTML tags only: `<b>`, `<br>`, `<code>`
- Avoid inline CSS styling
- Test on actual DSM device

## DSM 7.x Wizard API Reference

### Combobox with API
```json
{
    "type": "combobox",
    "subitems": [{
        "key": "pkgwizard_variable",
        "desc": "Field Label",
        "mode": "remote",
        "api_store": "SYNO.Core.Storage.Volume",
        "api_method": "list",
        "api_version": 1,
        "displayField": "volume",
        "valueField": "volume"
    }]
}
```

### Textfield with Validation
```json
{
    "type": "textfield",
    "subitems": [{
        "key": "pkgwizard_variable",
        "desc": "Field Label",
        "defaultValue": "default",
        "validator": {
            "allowBlank": false,
            "regex": {
                "expr": "/^pattern$/",
                "errorText": "Error message"
            }
        }
    }]
}
```

### Password Field
```json
{
    "type": "password",
    "subitems": [{
        "key": "pkgwizard_password",
        "desc": "Password",
        "defaultValue": ""
    }]
}
```

### Multiselect Field
```json
{
    "type": "multiselect",
    "subitems": [{
        "key": "pkgwizard_options",
        "desc": "Select Options",
        "defaultValue": "option1,option2"
    }]
}
```

## Testing Checklist

Before releasing SPK with wizard:

- [ ] Build SPK with validation enabled
- [ ] Verify JSON syntax: `python3 -m json.tool wizard.json`
- [ ] Verify INFO directive: `grep install_wizard spk/INFO`
- [ ] Inspect SPK contents: `tar -tf package.spk | grep WIZARD`
- [ ] Install on test DSM system
- [ ] Verify wizard displays (5 steps)
- [ ] Verify all fields are editable
- [ ] Verify default values populate correctly
- [ ] Test field validation (invalid IP, domain, email)
- [ ] Verify variables passed to postinst script
- [ ] Check installation logs for variable values
- [ ] Test with various network configurations
- [ ] Test with and without Cloudflare token

## References

### Official Documentation
- Synology DSM Developer Guide (DSM 7.x)
- Package Center SPK format specification
- WIZARD_UIFILES JSON schema

### Community Resources
- Synology Community Forums (Package Center)
- Example SPK packages with wizards from DSM
- GitHub repositories with SPK examples

### Common DSM APIs for Wizards
- `SYNO.Core.Storage.Volume` - List volumes
- `SYNO.Core.Network.Interface` - List network interfaces
- `SYNO.Core.User` - List users
- `SYNO.Core.Group` - List groups

## Version History

### v1.0 (Current)
- Initial wizard implementation
- 5 steps: Volume, Network, Domain/SSL, Cloudflare, Warning
- Simplified HTML formatting (no complex CSS)
- Build-time validation added
- API-driven volume selection

### Known Issues
- Complex CSS styling in `desc` fields may not render
- Wizard variables only available during install, not upgrade (by design)
- No way to modify wizard values after installation (use config files)

## Future Enhancements
- Consider adding upgrade wizard for configuration changes
- Validate IP addresses are within subnet range
- Add network interface discovery via API
- Pre-populate domain from reverse DNS lookup
