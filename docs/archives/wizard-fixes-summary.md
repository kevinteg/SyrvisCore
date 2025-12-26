# Installation Wizard Fixes - Summary

## Task Completed
Fixed HTML formatting and added validation to the SPK installation wizard to ensure it displays correctly on Synology DSM 7.x.

## Changes Made

### 1. Simplified HTML Formatting (Step 5)
**File:** `spk/WIZARD_UIFILES/install_uifile.json`

**Before:**
```html
<strong>IMPORTANT:</strong> ... <code style='display:block;padding:10px;background:#f5f5f5;border-radius:4px;font-family:monospace;'>command</code>
```

**After:**
```html
<b>IMPORTANT:</b> After installation completes, you must run:<br><br><code>sudo python3 /volume1/docker/syrviscore/bin/setup-privileges.py</code><br><br>This configures Docker permissions. Without this step, the syrvis CLI cannot manage containers.<br><br>You can also add this to Task Scheduler to run at boot.
```

**Rationale:** DSM's wizard engine may not support complex inline CSS styling. Simplified HTML improves compatibility.

### 2. Added Build Script Validation
**File:** `build-tools/build-spk.sh`

**New Validation Steps:**
1. **JSON Syntax Validation:** Validates wizard JSON is syntactically correct
2. **INFO File Check:** Verifies `install_wizard="yes"` directive exists
3. **Step Counting:** Reports number of wizard steps configured (5 steps)

**Output Example:**
```
[INFO] Validating wizard JSON syntax
[SUCCESS] Wizard JSON is valid
[INFO] Verifying INFO file wizard directive
[SUCCESS] INFO file has install_wizard directive
[INFO] Wizard has 5 steps configured
```

### 3. INFO File Verification
**File:** `spk/INFO`

**Confirmed Directive:**
```ini
install_wizard="yes"
```

This was already correct. The directive tells DSM to display the wizard from `WIZARD_UIFILES/install_uifile.json`.

### 4. Created Comprehensive Documentation
**File:** `docs/dsm-wizard-guide.md`

**Contents:**
- DSM 7.x wizard configuration guide
- Wizard file structure and JSON format
- All 5 wizard steps documented
- HTML formatting best practices
- Variable passing from wizard to scripts
- Troubleshooting guide
- Testing checklist
- DSM API reference for wizards

## Verification Results

### Build Validation
```bash
✓ Wizard JSON syntax is valid
✓ INFO file has install_wizard directive
✓ Wizard has 5 steps configured
✓ SPK package built successfully
```

### SPK Package Contents
```bash
✓ WIZARD_UIFILES/install_uifile.json present in SPK
✓ INFO file contains install_wizard="yes"
✓ JSON structure is valid
✓ Step 5 HTML is simplified (no complex CSS)
```

## Success Criteria Met

- [x] **HTML Simplified:** Step 5 uses basic HTML tags without complex CSS
- [x] **INFO Directive:** Confirmed `install_wizard="yes"` is present
- [x] **Build Validation:** Added JSON validation, INFO verification, step counting
- [x] **JSON Valid:** Wizard JSON passes syntax validation
- [x] **Documentation:** Created comprehensive DSM wizard guide
- [x] **SPK Verified:** Wizard files included in built package

## Testing Recommendations

### On Synology DSM 7.x:

1. **Install via Package Center:**
   - Upload SPK: `dist/syrviscore-0.0.1-noarch.spk`
   - Verify wizard displays with 5 steps
   - Check all fields are editable (except volume combobox)
   - Verify default values populate

2. **Test Wizard Steps:**
   - Step 1: Volume selection (combobox should list volumes)
   - Step 2: Network config (all 4 fields editable with defaults)
   - Step 3: Domain/email (required fields with validation)
   - Step 4: Cloudflare token (optional, can be blank)
   - Step 5: Warning page (HTML should render, not show as code)

3. **Verify Variable Passing:**
   - Check `/tmp/syrviscore-install.log` after installation
   - Confirm wizard variables show values, not `<not set>`
   - Expected variables:
     - `pkgwizard_volume`
     - `pkgwizard_network_interface`
     - `pkgwizard_network_subnet`
     - `pkgwizard_gateway_ip`
     - `pkgwizard_traefik_ip`
     - `pkgwizard_domain`
     - `pkgwizard_acme_email`
     - `pkgwizard_cloudflare_token` (optional)

4. **Test Field Validation:**
   - Invalid IP: Should reject (e.g., "999.999.999.999")
   - Invalid domain: Should reject (e.g., "not a domain")
   - Invalid email: Should reject (e.g., "notanemail")
   - Invalid CIDR: Should reject (e.g., "192.168.0/24")

## Alternative Directives to Try

If the wizard still doesn't display on DSM 7.x, try these alternatives in `spk/INFO`:

```ini
# Current (standard)
install_wizard="yes"

# Alternative 1
installer_type="wizard"

# Alternative 2
install_wizard_uifile="install_uifile.json"

# Alternative 3
wizard_uifile="install_uifile.json"
```

Test each one individually on the target DSM system.

## Known Limitations

1. **Wizard Only During Install:** Variables are only available during initial installation, not upgrades
2. **No Post-Install Modification:** Users cannot change wizard values after installation (use config files)
3. **HTML Rendering:** Complex HTML/CSS may not render; keep HTML simple
4. **Validation Client-Side:** Regex validation happens in browser; still validate in scripts

## Files Modified

1. `spk/WIZARD_UIFILES/install_uifile.json` - Simplified HTML in step 5
2. `build-tools/build-spk.sh` - Added wizard validation
3. `docs/dsm-wizard-guide.md` - Created (new file)
4. `docs/wizard-fixes-summary.md` - This file (new)

## Next Steps

1. **Test on DSM:** Install SPK on actual Synology NAS running DSM 7.x
2. **Verify Wizard Display:** Confirm all 5 steps show with proper formatting
3. **Check Logs:** Review `/tmp/syrviscore-install.log` for wizard variables
4. **Report Results:** Document whether wizard displays and variables pass correctly
5. **Try Alternatives:** If wizard doesn't display, test alternative INFO directives

## References

- **DSM Wizard Guide:** `docs/dsm-wizard-guide.md` (comprehensive reference)
- **Build Script:** `build-tools/build-spk.sh` (with validation)
- **Wizard File:** `spk/WIZARD_UIFILES/install_uifile.json` (5 steps)
- **INFO File:** `spk/INFO` (install_wizard="yes")

## Support

If issues persist after testing on DSM:
1. Check DSM system logs: `/var/log/synopkg.log`
2. Review Package Center error messages
3. Verify DSM version: `cat /etc/VERSION`
4. Check for SPK format errors: `synopkg verify <package.spk>`
5. Test with other SPK packages that have wizards
