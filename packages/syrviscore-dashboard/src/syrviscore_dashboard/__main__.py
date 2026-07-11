"""Entry point: ``python -m syrviscore_dashboard`` / ``syrviscore-dashboard`` runs uvicorn."""


def main() -> None:
    import uvicorn

    from .settings import get_settings

    settings = get_settings()
    uvicorn.run(
        "syrviscore_dashboard.app:create_app",
        factory=True,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
