#!/usr/bin/env python3
"""
SyrvisCore Docker Version Selection Tool

Interactive tool to discover available Docker image versions and create
a build configuration manifest with pinned versions.

Usage:
    ./select-docker-versions [--output build/config.yaml]
"""

import argparse
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
import yaml


class DockerHubClient:
    """Client for Docker Hub API v2."""

    BASE_URL = "https://hub.docker.com/v2"

    def get_tags(self, repository: str, limit: int = 10) -> List[Dict]:
        """
        Fetch tags for a Docker Hub repository.

        Args:
            repository: Repository name (e.g., "traefik" or "portainer/portainer-ce")
            limit: Maximum number of tags to fetch

        Returns:
            List of tag dictionaries with name and metadata
        """
        url = f"{self.BASE_URL}/repositories/{repository}/tags"
        params = {"page_size": limit, "ordering": "last_updated"}

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.RequestException as e:
            print(f"Error fetching tags for {repository}: {e}")
            return []

    def filter_stable_tags(self, tags: List[Dict]) -> List[Dict]:
        """
        Filter tags to only stable versions (exclude 'latest', 'dev', etc.).

        Prefers semantic version tags (e.g., v1.2.3, 2024.11.1) over build hashes.

        Args:
            tags: List of tag dictionaries

        Returns:
            Filtered list of stable version tags, sorted with semver-like first
        """
        import re

        excluded_keywords = ["latest", "dev", "nightly", "alpha", "beta", "rc", "experimental", "arm64", "amd64"]

        # Patterns for semantic versioning
        semver_pattern = re.compile(r'^v?\d+\.\d+(\.\d+)?(-\w+)?$')  # v1.2.3 or 2024.11.1

        stable_tags = []
        semver_tags = []

        for tag in tags:
            name = tag["name"].lower()

            # Skip if contains excluded keywords
            if any(keyword in name for keyword in excluded_keywords):
                continue

            # Skip if not a version-like tag (must start with digit or 'v')
            if not (name[0].isdigit() or name.startswith("v")):
                continue

            # Prioritize semver-like tags
            if semver_pattern.match(name):
                semver_tags.append(tag)
            else:
                stable_tags.append(tag)

        # Return semver tags first, then other stable tags
        return semver_tags + stable_tags


class VersionSelector:
    """Interactive version selector for Docker images."""

    def __init__(self):
        self.client = DockerHubClient()
        self.components = {
            "traefik": {
                "repository": "library/traefik",
                "description": "Traefik Reverse Proxy",
                "required": True,
            },
            "portainer": {
                "repository": "portainer/portainer-ce",
                "description": "Portainer Container Management",
                "required": True,
            },
            "cloudflared": {
                "repository": "cloudflare/cloudflared",
                "description": "Cloudflare Tunnel",
                "required": False,
            },
        }

    def fetch_versions(self, component: str) -> List[Dict]:
        """Fetch available versions for a component."""
        repo = self.components[component]["repository"]
        print(f"\n🔍 Fetching versions for {repo}...")

        tags = self.client.get_tags(repo, limit=50)
        stable_tags = self.client.filter_stable_tags(tags)

        return stable_tags[:10]  # Return top 10 stable versions

    def display_versions(self, component: str, versions: List[Dict]):
        """Display available versions in a formatted table."""
        info = self.components[component]

        print(f"\n{'='*70}")
        print(f"Component: {info['description']}")
        print(f"Repository: {info['repository']}")
        print(f"Required: {'Yes' if info['required'] else 'No (Optional)'}")
        print(f"{'='*70}")

        if not versions:
            print("⚠️  No versions found!")
            return

        print(f"\n{'#':<4} {'Tag':<20} {'Updated':<25} {'Size'}")
        print("-" * 70)

        for idx, version in enumerate(versions, 1):
            tag_name = version["name"]
            updated = version.get("last_updated", "Unknown")

            # Format date
            if updated != "Unknown":
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    updated = dt.strftime("%Y-%m-%d %H:%M UTC")
                except Exception:
                    pass

            # Get size if available
            size = "N/A"
            if version.get("full_size"):
                size_mb = version["full_size"] / (1024 * 1024)
                size = f"{size_mb:.1f} MB"

            print(f"{idx:<4} {tag_name:<20} {updated:<25} {size}")

    def select_version(self, component: str, versions: List[Dict]) -> Optional[str]:
        """Prompt user to select a version."""
        if not versions:
            return None

        while True:
            print(f"\nSelect version for {component}:")
            print("  Enter number (1-10) to select")
            print("  Enter 'c' to enter custom tag")
            print("  Enter 's' to skip (optional components only)")

            choice = input("\nYour choice: ").strip().lower()

            # Skip option
            if choice == "s":
                if not self.components[component]["required"]:
                    print(f"⏭️  Skipping {component}")
                    return None
                else:
                    print(f"❌ Cannot skip {component} - it's required!")
                    continue

            # Custom tag option
            if choice == "c":
                custom_tag = input("Enter custom tag: ").strip()
                if custom_tag:
                    return custom_tag
                continue

            # Number selection
            try:
                idx = int(choice)
                if 1 <= idx <= len(versions):
                    selected = versions[idx - 1]["name"]
                    print(f"✓ Selected: {selected}")
                    return selected
                else:
                    print(f"❌ Invalid choice. Enter 1-{len(versions)}")
            except ValueError:
                print("❌ Invalid input. Enter a number, 'c', or 's'")

    def run_interactive(self) -> Dict:
        """Run interactive version selection."""
        print("=" * 70)
        print("SyrvisCore Docker Version Selection Tool")
        print("=" * 70)
        print("\nThis tool will help you select Docker image versions for SyrvisCore.")
        print("You'll be able to choose from the latest stable releases.")

        selected_versions = {}

        for component in ["traefik", "portainer", "cloudflared"]:
            versions = self.fetch_versions(component)
            self.display_versions(component, versions)

            version = self.select_version(component, versions)
            if version:
                selected_versions[component] = {
                    "image": self.components[component]["repository"],
                    "tag": version,
                }

        return selected_versions

    def create_build_config(self, versions: Dict) -> Dict:
        """Create build configuration from selected versions."""
        config = {
            "metadata": {
                "syrviscore_version": "0.0.1",
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "created_by": "select-docker-versions",
            },
            "docker_images": {},
        }

        for component, info in versions.items():
            config["docker_images"][component] = {
                "image": info["image"],
                "tag": info["tag"],
                "full_image": f"{info['image']}:{info['tag']}",
            }

        return config

    def save_config(self, config: Dict, output_path: str):
        """Save configuration to YAML file."""
        try:
            # Ensure directory exists
            import os

            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            with open(output_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            print(f"\n✅ Configuration saved to: {output_path}")
        except Exception as e:
            print(f"\n❌ Error saving configuration: {e}")
            sys.exit(1)

    def update_compose_py(self, config: Dict):
        """Update DEFAULT_DOCKER_IMAGES in compose.py with selected versions."""
        import os
        import re

        # Find compose.py relative to this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        compose_path = os.path.join(script_dir, "..", "src", "syrviscore", "compose.py")
        compose_path = os.path.normpath(compose_path)

        if not os.path.exists(compose_path):
            print(f"\n⚠️  Could not find compose.py at {compose_path}")
            return

        try:
            with open(compose_path, "r") as f:
                content = f.read()

            # Build the new DEFAULT_DOCKER_IMAGES dict
            images = config.get("docker_images", {})
            new_dict_lines = ["DEFAULT_DOCKER_IMAGES = {"]

            for name, info in images.items():
                image = info.get("image", "")
                tag = info.get("tag", "")
                full_image = info.get("full_image", f"{image}:{tag}")
                desc = info.get("description", "")

                # Use library/ prefix for official images in compose, but not in the dict
                display_image = image.replace("library/", "")

                new_dict_lines.append(f'    "{name}": {{')
                new_dict_lines.append(f'        "image": "{display_image}",')
                new_dict_lines.append(f'        "tag": "{tag}",')
                new_dict_lines.append(f'        "full_image": "{display_image}:{tag}",')
                new_dict_lines.append(f'        "description": "{desc}",')
                new_dict_lines.append("    },")

            new_dict_lines.append("}")
            new_dict = "\n".join(new_dict_lines)

            # Replace the existing DEFAULT_DOCKER_IMAGES block
            # Match from "DEFAULT_DOCKER_IMAGES = {" to the closing "}" at the same indent level
            # This handles nested braces by counting brace depth
            start_marker = "DEFAULT_DOCKER_IMAGES = {"
            start_idx = content.find(start_marker)

            if start_idx != -1:
                # Find the matching closing brace
                brace_count = 0
                end_idx = start_idx
                for i, char in enumerate(content[start_idx:]):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = start_idx + i + 1
                            break

                # Replace the block
                content = content[:start_idx] + new_dict + content[end_idx:]

                with open(compose_path, "w") as f:
                    f.write(content)

                print(f"✅ Updated DEFAULT_DOCKER_IMAGES in compose.py")
            else:
                print(f"⚠️  Could not find DEFAULT_DOCKER_IMAGES in {compose_path}")

        except Exception as e:
            print(f"\n⚠️  Error updating compose.py: {e}")

    def display_summary(self, config: Dict):
        """Display summary of selected versions."""
        print("\n" + "=" * 70)
        print("CONFIGURATION SUMMARY")
        print("=" * 70)

        print(f"\nSyrvisCore Version: {config['metadata']['syrviscore_version']}")
        print(f"Created: {config['metadata']['created_at']}")

        print("\n📦 Docker Images:")
        for component, info in config["docker_images"].items():
            print(f"  • {component:<15} {info['full_image']}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Select Docker image versions for SyrvisCore build"
    )
    parser.add_argument(
        "--output",
        "-o",
        default="build/config.yaml",
        help="Output path for build configuration (default: build/config.yaml)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use latest stable versions without prompting",
    )

    args = parser.parse_args()

    selector = VersionSelector()

    if args.non_interactive:
        print("🤖 Non-interactive mode: selecting latest stable versions...")
        selected_versions = {}

        for component in ["traefik", "portainer", "cloudflared"]:
            versions = selector.fetch_versions(component)
            if versions:
                selected_versions[component] = {
                    "image": selector.components[component]["repository"],
                    "tag": versions[0]["name"],
                }
                print(f"  ✓ {component}: {versions[0]['name']}")
    else:
        selected_versions = selector.run_interactive()

    if not selected_versions:
        print("\n❌ No versions selected. Exiting.")
        sys.exit(1)

    # Create build configuration
    config = selector.create_build_config(selected_versions)

    # Display summary
    selector.display_summary(config)

    # Save to file
    selector.save_config(config, args.output)

    # Update compose.py with the selected versions
    selector.update_compose_py(config)

    print("\n✨ Done! You can now use this configuration to build an SPK.")
    print("\nNext steps:")
    print(f"  1. Review: {args.output}")
    print("  2. Run: ./build-tools/build-python-package.sh")
    print("  3. Run: ./build-tools/build-spk.sh")


if __name__ == "__main__":
    main()
