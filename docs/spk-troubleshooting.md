# SPK Installation Troubleshooting

This guide helps diagnose and fix issues with SyrvisCore SPK installation on Synology NAS.

## Log Files

SyrvisCore creates detailed log files during installation:

| Log File | Contents |
|----------|----------|
| `/tmp/syrviscore-install.log` | Main installation log with all steps |
| `/tmp/syrviscore-pip.log` | Pip package installation output |

### Viewing Logs

```bash
# View full installation log
cat /tmp/syrviscore-install.log

# View last 50 lines
tail -50 /tmp/syrviscore-install.log

# View pip-specific log
cat /tmp/syrviscore-pip.log

# Follow logs in real-time (during installation)
tail -f /tmp/syrviscore-install.log
```

## Common Issues

### 1. Python 3 Not Found

**Symptom:**
```
[ERROR] Python 3 not found
```

**Solution:**
- DSM 7.0+ should include Python 3 by default
- If missing, check if Python package is available in Package Center
- Verify with: `python3 --version`

### 2. Pip Install Failed

**Symptom:**
```
[ERROR] Pip install failed! Check /tmp/syrviscore-pip.log for details
```

**Diagnosis:**
```bash
cat /tmp/syrviscore-pip.log
```

**Common causes:**
- Missing wheel files in the SPK package
- Incompatible wheel format for your architecture
- Corrupted download

**Solutions:**
- Re-download the SPK from the [latest release](https://github.com/kevinteg/SyrvisCore/releases)
- Check your NAS architecture (x86_64, armv8, etc.)

### 3. syrvisctl Command Not Found After Install

**Symptom:**
```
[ERROR] syrvisctl command not found after install
```

**Diagnosis:**
```bash
# Check if venv was created
ls -la /var/packages/syrviscore/target/venv/

# Check if bin directory has the command
ls -la /var/packages/syrviscore/target/venv/bin/
```

**Solutions:**
- Check pip installation log for errors
- Ensure sufficient disk space
- Try reinstalling the package

### 4. Insufficient Disk Space

**Symptom:**
```
[ERROR] Insufficient disk space
```

**Solution:**
- SyrvisCore requires at least 500MB free space
- Check available space: `df -h /volume1`
- Free up space and retry installation

### 5. Permission Denied

**Symptom:**
```
[ERROR] Directory is not writable
```

**Solutions:**
- Ensure Docker is installed from Package Center
- The installation directory should be on a volume with Docker support
- Check volume permissions in DSM Control Panel

### 6. Symlink Creation Failed

**Symptom:**
```
[ERROR] Failed to create symlink
```

**Diagnosis:**
```bash
ls -la /usr/local/bin/syrvisctl
```

**Solutions:**
- Remove existing file: `sudo rm -f /usr/local/bin/syrvisctl`
- Retry installation

## Post-Installation Issues

### syrvisctl install Fails

If `syrvisctl install` fails after SPK installation:

```bash
# Check syrvisctl is working
syrvisctl --version

# Check network connectivity to GitHub
curl -I https://github.com

# View syrvisctl logs
syrvisctl install --verbose
```

### syrvis setup Fails

If `syrvis setup` fails:

```bash
# Check Docker is running
docker ps

# Check if user is in docker group
groups

# Verify service installation
syrvisctl list
```

## Getting Help

### Collect Debug Information

Before reporting an issue, collect this information:

```bash
# System info
uname -a
cat /etc/VERSION
python3 --version

# Installation logs
cat /tmp/syrviscore-install.log
cat /tmp/syrviscore-pip.log

# Package status
ls -la /var/packages/syrviscore/target/
syrvisctl --version 2>&1 || echo "syrvisctl not available"
```

### Report an Issue

1. Go to [GitHub Issues](https://github.com/kevinteg/SyrvisCore/issues)
2. Click "New Issue"
3. Include:
   - DSM version
   - NAS model
   - Contents of log files
   - Steps to reproduce

## Manual Cleanup

If installation fails and you need to clean up:

```bash
# Remove package via Package Center (preferred)
# OR manually:

# Remove venv
sudo rm -rf /var/packages/syrviscore/target/venv

# Remove symlink
sudo rm -f /usr/local/bin/syrvisctl

# Clear logs
rm -f /tmp/syrviscore-install.log
rm -f /tmp/syrviscore-pip.log
```

Then reinstall the SPK through Package Center.
