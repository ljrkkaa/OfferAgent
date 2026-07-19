from offline_qualification_bootstrap import install_offline_qualification_guard

install_offline_qualification_guard()

from offeragent_harness.runtime.development_composition import worker_main  # noqa: E402

raise SystemExit(worker_main())
