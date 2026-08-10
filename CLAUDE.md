# Project notes for etekcity-scale-daemon

## Upstream/related repos to watch

- **etekcity_esf551_ble** --
  https://github.com/ronnnnnnnnnnnnn/etekcity_esf551_ble -- this daemon's
  actual BLE protocol dependency (a real PyPI package,
  `etekcity_esf551_ble>=0.8.0,<1.0.0` in `pyproject.toml`, not a
  git+https pin like the sibling BLE daemons in this family). Does all
  the BLE protocol/reverse-engineering work this daemon relies on; a new
  release there (new scale model support, a protocol fix) needs a version
  bump in `pyproject.toml` to actually reach this daemon.

- This project is the architecture template the rest of this daemon
  family (`etekcity-bp-daemon`, `trividia-truemetrix-daemon`) was
  deliberately modeled on -- config/storage/alerting/MQTT/pruning/
  Docker/CI patterns, notification throttling shapes, etc. If a bug gets
  fixed or a pattern improved in one of those siblings, it's worth
  checking whether the same fix applies here too, since it was very
  likely copied in the other direction originally.

## Verification status

See the README for current hardware-verification status and the Docker
section's CI-verification notes.
