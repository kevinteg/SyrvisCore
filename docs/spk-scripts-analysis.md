# SPK Lifecycle Scripts Analysis

## Overview

SyrvisCore uses a **two-phase installation architecture**:
- **Phase 1 (Unprivileged)**: SPK scripts run as package user, install core functionality
- **Phase 2 (Privileged)**: User manually runs `setup-privileges.py` as root for Docker access

This document analyzes which SPK lifecycle scripts are needed and what each should do.

## Script Analysis

### ✅ KEEP: preinst (Pre-Installation)

**Current Purpose:** Validates system requirements before installation

**Privilege Level:** Unprivileged (package user)

**What it does:**
1. Checks if Docker package is installed
2. Verifies Docker is running  
3. Confirms Python 3 is available

**Why we need it:**
- Catches missing dependencies before installation starts
- Prevents failed installations due to missing Docker
- All checks are read-only operations (no system modifications)

**Changes needed:**
- ✅ Already has enhanced logging
- ✅ POSIX-compatible
- ✅ No privileged operations

**Status:** **Keep as-is**

---

### ✅ KEEP: postinst (Post-Installation)

**Current Purpose:** Main installation script - installs SyrvisCore files and CLI

**Privilege Level:** Unprivileged (package user)

**What it does:**
1. Creates directory structure in user's volume
2. Installs Python CLI in virtual environment
3. Copies build configuration
4. Generates .env from wizard variables
5. Creates acme.json for SSL certificates
6. Creates CLI wrapper script
7. Creates installation manifest
8. Copies setup-privileges.py
9. Generates Task Scheduler template

**Why we need it:**
- Core installation logic
- All operations are unprivileged (writes to user's directories)
- No Docker socket access needed yet
- Creates tools for Phase 2 (setup-privileges.py)

**Changes needed:**
- ✅ Already has enhanced logging
- ✅ POSIX-compatible
- ✅ No privileged operations

**Status:** **Keep as-is**

---

### ✅ KEEP: preuninst (Pre-Uninstallation)

**Current Purpose:** Cleanup before package removal

**Privilege Level:** **Mixed** - Some operations need root, script handles both cases

**What it does:**
1. Detects installation directory
2. Stops services gracefully via CLI
3. Cleans up privileged system resources:
   - Removes startup script (`/usr/local/etc/rc.d/S99syrviscore.sh`)
   - Removes global CLI symlink (`/usr/local/bin/syrvis`)
4. Warns if not running as root

**Why we need it:**
- Gracefully stops services before uninstallation
- Cleans up privileged resources that Phase 2 created
- Prevents orphaned system files

**Current Issue:**
- Tries to do privileged operations but may not have root access
- Synology may or may not run this as root (varies by DSM version)

**Changes needed:**
- ✅ Already has enhanced logging
- ✅ Already handles non-root case gracefully
- ✅ No changes needed

**Status:** **Keep as-is** (handles both privileged and unprivileged execution)

---

### ✅ KEEP: postuninst (Post-Uninstallation)

**Current Purpose:** Final cleanup after package removal

**Privilege Level:** **Privileged** (Synology runs this as root)

**What it does:**
1. Removes startup script (if still present)
2. Removes global CLI symlink (if still present)
3. Resets Docker socket permissions to default
4. Handles data directory based on user choice:
   - **Keep data** (default): Preserves data, removes only CLI
   - **Remove data**: Removes entire installation directory
5. Removes user from docker/administrators groups
6. Cleans up temporary files
7. Creates UNINSTALLED.txt notice if data preserved

**Why we need it:**
- **Privileged cleanup**: Synology DSM runs postuninst as root
- Ensures all system resources are cleaned up
- Provides user choice for data preservation
- Prevents orphaned Docker socket permissions

**Current Issues:**
- Contains privileged operations that violate the two-phase principle
- BUT: This is the CORRECT place for privileged cleanup during uninstall
- Synology's architecture explicitly allows this

**Architectural Decision:**
**EXCEPTION TO TWO-PHASE RULE**: postuninst is the **only** SPK script that performs privileged operations, because:
1. Synology DSM explicitly runs postuninst as root
2. It's the cleanup counterpart to Phase 2's setup-privileges.py
3. Users expect uninstall to fully clean up system resources
4. Alternative would require manual cleanup steps (poor UX)

**Changes needed:**
- ⚠️ Needs enhanced logging (currently has basic logging)
- ⚠️ Uses deprecated synogroup commands (should use modern synogroup API)
- ⚠️ Docker socket reset may break other packages

**Status:** **Keep but needs updates** (see recommendations below)

---

### ✅ KEEP: preupgrade (Pre-Upgrade)

**Current Purpose:** Prepares for upgrade by backing up configuration

**Privilege Level:** Unprivileged (package user)

**What it does:**
1. Finds existing installation
2. Reads current version from manifest
3. Stops running services via CLI
4. Backs up configuration files (.env, docker-compose.yaml, manifest)
5. Stores installation directory for postupgrade

**Why we need it:**
- Ensures services are stopped before upgrade
- Backs up user configuration
- Provides rollback capability
- All operations are read-only or write to user directories

**Changes needed:**
- ⚠️ Needs enhanced logging (currently has basic logging)
- ⚠️ POSIX compatibility (uses some bashisms)

**Status:** **Keep but needs logging updates**

---

### ✅ KEEP: postupgrade (Post-Upgrade)

**Current Purpose:** Upgrades SyrvisCore to new version

**Privilege Level:** Unprivileged (package user)

**What it does:**
1. Detects existing installation
2. Creates version backup directory
3. Backs up old manifest and .env
4. Upgrades Python CLI (removes old venv, creates new one)
5. Updates build configuration
6. **Preserves .env** (does NOT regenerate from wizard)
7. Updates CLI wrapper
8. Updates manifest with upgrade information
9. Updates setup-privileges.py
10. Updates Task Scheduler template

**Why we need it:**
- Cannot reuse postinst because upgrades need different logic:
  - Must preserve existing .env configuration
  - Must track previous version for rollback
  - Must update manifest, not create new one
- All operations are unprivileged

**Changes needed:**
- ⚠️ Needs enhanced logging (currently minimal)
- ⚠️ Manifest parsing is fragile (uses grep/cut)

**Status:** **Keep but needs logging updates**

---

### ✅ KEEP: start-stop-status (Service Control)

**Current Purpose:** Handles DSM service control commands

**Privilege Level:** Unprivileged (package user)

**What it does:**
1. Handles `start` command: Explains services are user-managed
2. Handles `stop` command: Explains services are user-managed
3. Handles `status` command: Checks if setup is complete via manifest

**Why we need it:**
- Required by Synology DSM for Package Center integration
- Provides status indicator in DSM UI
- Explains that services are managed via CLI, not DSM
- Uses manifest's `setup_complete` flag to show installation state

**Changes needed:**
- ✅ Already has enhanced logging
- ✅ POSIX-compatible

**Status:** **Keep as-is**

---

## Summary & Recommendations

### Scripts to Keep (7/7)

All scripts serve a purpose and should be retained:

| Script | Privilege | Purpose | Changes Needed |
|--------|-----------|---------|----------------|
| preinst | Unprivileged | Pre-install checks | ✅ None (already updated) |
| postinst | Unprivileged | Main installation | ✅ None (already updated) |
| preuninst | Mixed | Pre-uninstall cleanup | ✅ None (already updated) |
| postuninst | **Privileged** | Final cleanup | ⚠️ Enhanced logging needed |
| preupgrade | Unprivileged | Pre-upgrade backup | ⚠️ Enhanced logging needed |
| postupgrade | Unprivileged | Upgrade installation | ⚠️ Enhanced logging needed |
| start-stop-status | Unprivileged | Status reporting | ✅ None (already updated) |

### Two-Phase Architecture Compliance

**Compliant:**
- preinst ✅
- postinst ✅
- preupgrade ✅
- postupgrade ✅
- start-stop-status ✅

**Mixed (by design):**
- preuninst ⚠️ - Handles both privileged and unprivileged gracefully

**Exception (justified):**
- postuninst ⚠️ - Privileged operations are appropriate for uninstall cleanup
  - This is the **only** script that performs privileged operations
  - Synology DSM explicitly runs this as root
  - Provides clean uninstall experience
  - Alternative would require manual cleanup (poor UX)

### Priority Action Items

1. **Update postuninst with enhanced logging** (high priority)
   - Add consistent log functions
   - Log each cleanup operation
   - Log privilege level
   - Make it match the logging standard

2. **Update preupgrade with enhanced logging** (medium priority)
   - Add structured logging
   - Log backup operations
   - Make POSIX-compatible

3. **Update postupgrade with enhanced logging** (medium priority)
   - Add structured logging
   - Log each upgrade step
   - Improve manifest parsing robustness

### Future Considerations

**Data Handling in postuninst:**
- Current: Preserves data by default, wizard option not implemented yet
- Future: Add wizard option for "Remove all data" during uninstall
- Keep current safe default (preserve data)

**Docker Socket Reset:**
- Current: postuninst resets socket to root:root 666
- Risk: May break other applications using Docker socket
- Recommendation: Only reset if we modified it (check if syrvis-bot was in docker group)

**User Account Cleanup:**
- Current: Removes syrvis-bot from groups, keeps user account
- Future: Consider full user removal with wizard option
- Keep current conservative approach (preserve user for reinstall)

## Conclusion

**All 7 SPK lifecycle scripts are needed and serve distinct purposes.**

The two-phase architecture is maintained with one justified exception:
- **postuninst** performs privileged cleanup because Synology DSM runs it as root
- This is the architectural balance: Phase 2 setup needs manual root execution, but cleanup can be automatic

No scripts should be removed. Focus on updating logging in the 3 upgrade/uninstall scripts to match the standard used in preinst, postinst, preuninst, and start-stop-status.
