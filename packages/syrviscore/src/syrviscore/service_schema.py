"""
Service definition schema for Layer 2 services.

Each service is defined by a syrvis-service.yaml file in its git repository.
This module provides the dataclasses for parsing and validating these definitions.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class TraefikConfig:
    """Traefik routing configuration for a service."""

    enabled: bool = True
    subdomain: str = ""
    port: int = 80
    middlewares: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TraefikConfig":
        """Create TraefikConfig from dictionary."""
        if not data:
            return cls(enabled=False)
        return cls(
            enabled=data.get("enabled", True),
            subdomain=data.get("subdomain", ""),
            port=data.get("port", 80),
            middlewares=data.get("middlewares", []),
        )


@dataclass
class ConfigTemplate:
    """Template file to copy during service installation."""

    source: str
    dest: str

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "ConfigTemplate":
        """Create ConfigTemplate from dictionary."""
        return cls(
            source=data.get("source", ""),
            dest=data.get("dest", ""),
        )


@dataclass
class ServiceDefinition:
    """Complete service definition from syrvis-service.yaml."""

    name: str
    version: str
    image: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    container_name: str = ""
    traefik: TraefikConfig = field(default_factory=TraefikConfig)
    environment: List[str] = field(default_factory=list)
    volumes: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    config_templates: List[ConfigTemplate] = field(default_factory=list)
    restart: str = "unless-stopped"
    # Source information (set after loading)
    source_path: Optional[Path] = None
    source_url: Optional[str] = None

    def __post_init__(self):
        """Set defaults after initialization."""
        if not self.container_name:
            self.container_name = self.name
        if not self.networks:
            self.networks = ["proxy"]
        elif "proxy" not in self.networks:
            self.networks.append("proxy")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceDefinition":
        """Create ServiceDefinition from dictionary."""
        # Validate required fields
        required = ["name", "version", "image"]
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        return cls(
            name=data["name"],
            version=data["version"],
            image=data["image"],
            description=data.get("description", ""),
            author=data.get("author", ""),
            homepage=data.get("homepage", ""),
            container_name=data.get("container_name", data["name"]),
            traefik=TraefikConfig.from_dict(data.get("traefik")),
            environment=data.get("environment", []),
            volumes=data.get("volumes", []),
            networks=data.get("networks", []),
            depends_on=data.get("depends_on", []),
            config_templates=[
                ConfigTemplate.from_dict(t) for t in data.get("config_templates", [])
            ],
            restart=data.get("restart", "unless-stopped"),
        )

    @classmethod
    def from_yaml(cls, yaml_path: Path) -> "ServiceDefinition":
        """Load service definition from YAML file."""
        if not yaml_path.exists():
            raise FileNotFoundError(f"Service definition not found: {yaml_path}")

        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        if not data:
            raise ValueError(f"Empty service definition: {yaml_path}")

        service = cls.from_dict(data)
        service.source_path = yaml_path.parent
        return service

    def to_dict(self) -> Dict[str, Any]:
        """Convert service definition to dictionary."""
        result = {
            "name": self.name,
            "version": self.version,
            "image": self.image,
            "container_name": self.container_name,
            "restart": self.restart,
        }

        if self.description:
            result["description"] = self.description
        if self.author:
            result["author"] = self.author
        if self.homepage:
            result["homepage"] = self.homepage

        if self.traefik.enabled:
            result["traefik"] = {
                "enabled": self.traefik.enabled,
                "subdomain": self.traefik.subdomain,
                "port": self.traefik.port,
            }
            if self.traefik.middlewares:
                result["traefik"]["middlewares"] = self.traefik.middlewares

        if self.environment:
            result["environment"] = self.environment
        if self.volumes:
            result["volumes"] = self.volumes
        if self.networks:
            result["networks"] = self.networks
        if self.depends_on:
            result["depends_on"] = self.depends_on
        if self.config_templates:
            result["config_templates"] = [
                {"source": t.source, "dest": t.dest} for t in self.config_templates
            ]

        return result


def load_service_definition(path: Path) -> ServiceDefinition:
    """Load a service definition from a directory or YAML file.

    Args:
        path: Path to service directory or syrvis-service.yaml file

    Returns:
        Parsed ServiceDefinition

    Raises:
        FileNotFoundError: If service definition not found
        ValueError: If service definition is invalid
    """
    if path.is_dir():
        yaml_path = path / "syrvis-service.yaml"
    else:
        yaml_path = path

    return ServiceDefinition.from_yaml(yaml_path)
