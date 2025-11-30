#!/usr/bin/env python3
"""
SyrvisCore Docker Version Selection Tool

Interactive tool to discover available Docker image versions and create
a build configuration manifest with pinned versions.

Usage:
    ./select-docker-versions [--output build/config.yaml]
"""

import argparse
import json
import sys
from datetime import datetime
from typing import Dict, List, Optional

try:
    import requests
    import yaml
except ImportError:
    print("Error: Required packages not installed.")
    print("Run: pip install requests pyyaml")
    sys.exit(1)


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
        params = {"page_size": limit, "ordering": "-last_updated"}
        
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
        
        Args:
            tags: List of tag dictionaries
            
        Returns:
            Filtered list of stable version tags
        """
        excluded_keywords = ["latest", "dev", "nightly", "alpha", "beta", "rc", "experimental"]
        
        stable_tags = []
        for tag in tags:
            name = tag["name"].lower()
            
            # Skip if contains excluded keywords
            if any(keyword in name for keyword in excluded_keywords):
                continue
            
            # Skip if not a version-like tag (must start with digit or 'v')
            if not (name[0].isdigit() or name.startswith("v")):
                continue
                
            stable_tags.append(tag)
        
        return stable_tags


class VersionSelector:
    """Interactive version selector for Docker images."""

    def __init__(self):
        self.client = DockerHubClient()
        self.components = {
            "traefik": {
                "repository": "traefik",
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
                "syrviscore_version": "0.1.0-dev",
                "created_at": datetime.utcnow().isoformat() + "Z",
                "created_by": "select-docker-versions",
            },
            "docker_images": {},
            "python_dependencies": {
                "click": ">=8.1.0",
                "pyyaml": ">=6.0",
                "requests": ">=2.31.0",
            },
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
        
        print("\n🐍 Python Dependencies:")
        for pkg, version in config["python_dependencies"].items():
            print(f"  • {pkg:<15} {version}")


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
    
    print("\n✨ Done! You can now use this configuration to build an SPK.")
    print(f"\nNext steps:")
    print(f"  1. Review: {args.output}")
    print(f"  2. Run: ./build-tools/build-spk")


if __name__ == "__main__":
    main()
