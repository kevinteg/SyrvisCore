# syrvis-gollum

A Gollum wiki service for SyrvisCore.

## What is Gollum?

[Gollum](https://github.com/gollum/gollum) is a simple wiki system built on top of Git. Every page is a file in the repository, and all changes are tracked via Git commits.

## Installation

```bash
syrvis service add https://github.com/yourusername/syrvis-gollum.git
```

Or for local testing:
```bash
# Copy to services directory
cp -r examples/syrvis-gollum $SYRVIS_HOME/services/gollum

# Then manually generate configs and start
```

## Access

Once installed, access your wiki at: `https://wiki.yourdomain.com`

## Data Storage

Wiki data is stored in `$SYRVIS_HOME/data/gollum/wiki/`

Since Gollum uses Git for storage, you can:
- Clone the wiki repository for backup
- Make changes locally and push them
- Use standard Git tools for history

## Configuration

Edit `syrvis-service.yaml` to customize:
- `traefik.subdomain` - Change the subdomain (default: wiki)
- `environment` - Set author name/email for commits
