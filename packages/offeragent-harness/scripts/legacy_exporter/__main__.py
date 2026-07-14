try:
    from .exporter import main
except ImportError:  # Supports `python scripts/legacy_exporter --help`.
    from exporter import main

raise SystemExit(main())
