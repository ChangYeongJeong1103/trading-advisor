"""Allow `python -m anomaly.replay <event_id>` to invoke cli.main()."""

from .cli import main

raise SystemExit(main())
