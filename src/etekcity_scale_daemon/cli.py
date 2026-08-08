"""Command-line entry point and daemon run loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import aiomqtt
from bleak import BleakScanner
from etekcity_esf551_ble import (
    HEART_RATE_KEY,
    IMPEDANCE_500KHZ_KEY,
    IMPEDANCE_KEY,
    SCALE_CLASSES,
    WEIGHT_KEY,
    BluetoothScanningMode,
    ScaleData,
    ScaleModel,
    detect_model,
)

from ._version import __version__
from .config import (
    DEFAULT_API_CONFIG,
    DEFAULT_MQTT_CONFIG,
    DEFAULT_PROFILES_CONFIG,
    ApiConfig,
    ConfigError,
    DaemonConfig,
    MqttConfig,
    ProfilesConfig,
    load_alert_config,
    load_api_config,
    load_config,
    load_mqtt_config,
    load_profile_biometrics,
    load_profiles_config,
    load_report_config,
    persist_discovered_scale,
)
from .storage import MeasurementStore, get_distinct_profiles, set_measurement_profile

_LOGGER = logging.getLogger("etekcity_scale_daemon")

_GATT_MODELS = (ScaleModel.ESF551, ScaleModel.ESF24, ScaleModel.EFSA591S)


async def discover_scale(
    adapter: str | None, timeout: float = 60.0
) -> tuple[str, ScaleModel]:
    """Scan for the first advertisement matching a supported scale.

    Args:
        adapter: Optional BLE adapter to scan with (Linux only).
        timeout: Seconds to scan before giving up.

    Returns:
        The discovered scale's BLE address and model.

    Raises:
        TimeoutError: If no supported scale is found within ``timeout``.
    """
    found: asyncio.Future[tuple[str, ScaleModel]] = (
        asyncio.get_running_loop().create_future()
    )

    def on_advertisement(device, adv) -> None:
        model = detect_model(adv.local_name, adv.manufacturer_data, device.address)
        if model is not None and not found.done():
            found.set_result((device.address, model))

    scanner_kwargs = {"adapter": adapter} if adapter else {}
    _LOGGER.info(
        "No scale configured yet - scanning for a supported scale "
        "(step on it now)..."
    )
    async with BleakScanner(on_advertisement, **scanner_kwargs):
        return await asyncio.wait_for(found, timeout)


def _measurement_to_row(
    data: ScaleData, address: str, model: str
) -> dict[str, object]:
    """Flatten a ScaleData notification into storage-ready fields."""
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "address": address,
        "model": model,
        "weight_kg": data.measurements.get(WEIGHT_KEY),
        "impedance_ohms": data.measurements.get(IMPEDANCE_KEY),
        "impedance_500khz_ohms": data.measurements.get(IMPEDANCE_500KHZ_KEY),
        "heart_rate_bpm": data.measurements.get(HEART_RATE_KEY),
        "display_unit": (
            data.display_unit.name if data.display_unit is not None else None
        ),
    }


@asynccontextmanager
async def _mqtt_connection(mqtt_config: MqttConfig):
    """Yield a connected MQTT client, or None if disabled or unreachable.

    A broker connection failure is logged and treated as non-fatal: BLE
    scale recording to the local database is the daemon's primary job and
    must not be blocked by an MQTT outage.

    Args:
        mqtt_config: Parsed [mqtt] configuration.

    Yields:
        A connected ``aiomqtt.Client``, or None if MQTT is disabled or the
        broker could not be reached.
    """
    if not mqtt_config.enabled:
        yield None
        return

    tls_context = ssl.create_default_context() if mqtt_config.use_tls else None
    try:
        async with aiomqtt.Client(
            hostname=mqtt_config.host,
            port=mqtt_config.port,
            username=mqtt_config.username or None,
            password=mqtt_config.password or None,
            tls_context=tls_context,
        ) as client:
            _LOGGER.info(
                "Connected to MQTT broker %s:%s", mqtt_config.host, mqtt_config.port
            )
            yield client
    except aiomqtt.MqttError as exc:
        _LOGGER.warning(
            "Could not connect to MQTT broker %s:%s (%s) -- continuing without "
            "MQTT publishing",
            mqtt_config.host,
            mqtt_config.port,
            exc,
        )
        yield None


async def _publish_measurement(
    client: aiomqtt.Client, mqtt_config: MqttConfig, address: str, row: dict[str, object]
) -> None:
    """Publish one measurement to MQTT as a JSON payload.

    Failures are logged, not raised -- a broker hiccup shouldn't be allowed
    to propagate into the scale's notification callback.

    Args:
        client: A connected MQTT client.
        mqtt_config: Supplies the topic prefix, QoS, and retain flag.
        address: The scale's BLE address, used as the topic's last segment.
        row: The measurement fields, as built by ``_measurement_to_row``.
    """
    topic = f"{mqtt_config.topic_prefix}/{address}/state"
    try:
        await client.publish(
            topic, json.dumps(row), qos=mqtt_config.qos, retain=mqtt_config.retain
        )
    except aiomqtt.MqttError as exc:
        _LOGGER.warning("MQTT publish to %s failed: %s", topic, exc)


_NTFY_RETRY_DELAYS_SECONDS = (1, 2)
_NTFY_REQUEST_TIMEOUT_SECONDS = 5
# Worst case: every attempt hangs for the full per-request timeout, plus
# every retry delay in between -- used to size the daemon's shutdown wait
# for a still-retrying notification (see run_daemon's finally block).
_NTFY_MAX_RETRY_SECONDS = _NTFY_REQUEST_TIMEOUT_SECONDS * (
    len(_NTFY_RETRY_DELAYS_SECONDS) + 1
) + sum(_NTFY_RETRY_DELAYS_SECONDS)


async def _notify_via_ntfy(
    row_id: int, weight_kg: float | None, profiles_config: ProfilesConfig
) -> None:
    """Announce a new reading via ntfy, with one HTTP action button per profile.

    Each action calls back into the local HTTP API's ``/assign-profile``
    endpoint when tapped, so the actual tagging happens later (whenever a
    human responds), not here.

    Retries up to twice (after 1s, then 2s) on a connection failure or a
    5xx server response, since those are often transient (e.g. the ntfy
    server restarting) -- without this, a brief outage at exactly the
    wrong moment meant that reading could only ever be tagged manually. A
    4xx response is never retried, since trying again won't fix a bad
    token or malformed request.

    Args:
        row_id: The measurement's primary key, to tag once a profile is chosen.
        weight_kg: The recorded weight, for the notification body.
        profiles_config: Supplies the profile names, ntfy target, and the
            API base URL the action buttons call back into.
    """
    callback_base = f"{profiles_config.api_base_url}/assign-profile"
    headers = {}
    if profiles_config.ntfy_token:
        headers["Authorization"] = f"Bearer {profiles_config.ntfy_token}"

    # JSON publishing requires POSTing to the server's root URL with the
    # topic in the body, not to <server>/<topic> like a plain-text publish.
    clean_url = profiles_config.ntfy_url.rstrip("/")
    ntfy_root, _, topic = clean_url.rpartition("/")
    if not ntfy_root:
        ntfy_root = clean_url

    weight_text = f"{weight_kg:.2f} kg" if weight_kg is not None else "unknown weight"
    payload = {
        "topic": topic,
        "title": "New scale reading",
        "message": f"{weight_text} -- who was this?",
        "actions": [
            {
                "action": "http",
                "label": name,
                "url": f"{callback_base}?id={row_id}&profile={name}",
                "method": "POST",
                "clear": True,
            }
            for name in profiles_config.names
        ],
    }

    last_error = None
    for attempt in range(len(_NTFY_RETRY_DELAYS_SECONDS) + 1):
        if attempt > 0:
            await asyncio.sleep(_NTFY_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            timeout = aiohttp.ClientTimeout(total=_NTFY_REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(ntfy_root, json=payload, headers=headers) as response:
                    if response.status >= 500:
                        last_error = f"HTTP {response.status}: {await response.text()}"
                        continue
                    if response.status >= 400:
                        _LOGGER.warning(
                            "ntfy publish failed with HTTP %s: %s",
                            response.status,
                            await response.text(),
                        )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = str(exc) or repr(exc)
            continue

    _LOGGER.warning(
        "ntfy publish failed after %d attempt(s): %s",
        len(_NTFY_RETRY_DELAYS_SECONDS) + 1,
        last_error,
    )


async def _prompt_via_dunstify(
    db_path: str,
    row_id: int,
    weight_kg: float | None,
    profiles_config: ProfilesConfig,
) -> None:
    """Ask locally (via dunstify) which profile a reading belongs to.

    Blocks (within this background task, not the caller) until an action is
    chosen or the timeout elapses, then tags the row directly -- there's no
    HTTP API to call back into in this path, so the answer is applied here.

    Args:
        db_path: Path to the SQLite database file.
        row_id: The measurement's primary key to tag.
        weight_kg: The recorded weight, for the notification body.
        profiles_config: Supplies the profile names and timeout.
    """
    weight_text = f"{weight_kg:.2f} kg" if weight_kg is not None else "unknown weight"
    args = ["dunstify", "-t", str(profiles_config.dunstify_timeout_seconds * 1000)]
    for name in profiles_config.names:
        args += ["--action", f"{name},{name}"]
    args += ["New scale reading", f"{weight_text} -- who was this?"]

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=profiles_config.dunstify_timeout_seconds + 5
        )
    except (OSError, asyncio.TimeoutError) as exc:
        _LOGGER.warning("dunstify profile prompt failed: %s", exc)
        return

    chosen = stdout.decode().strip()
    if chosen not in profiles_config.names:
        _LOGGER.info("No profile chosen for reading %s (timed out or dismissed)", row_id)
        return

    if set_measurement_profile(db_path, row_id, chosen):
        _LOGGER.info("Tagged reading %s as profile %s", row_id, chosen)


async def _prompt_for_profile(
    db_path: str,
    row_id: int,
    weight_kg: float | None,
    profiles_config: ProfilesConfig,
    api_config: ApiConfig,
) -> None:
    """Dispatch to ntfy (if the API is reachable) or dunstify (if not).

    Args:
        db_path: Path to the SQLite database file.
        row_id: The measurement's primary key to tag.
        weight_kg: The recorded weight, for the notification body.
        profiles_config: Supplies profile names and per-path settings.
        api_config: Determines which path is used -- ntfy's action buttons
            have nothing to call back to without the API running.
    """
    if api_config.enabled:
        await _notify_via_ntfy(row_id, weight_kg, profiles_config)
    else:
        await _prompt_via_dunstify(db_path, row_id, weight_kg, profiles_config)


async def run_daemon(
    config: DaemonConfig,
    once: bool = False,
    once_timeout: int = 60,
    mqtt_config: MqttConfig = DEFAULT_MQTT_CONFIG,
    profiles_config: ProfilesConfig = DEFAULT_PROFILES_CONFIG,
    api_config: ApiConfig = DEFAULT_API_CONFIG,
) -> bool:
    """Connect to the configured (or newly discovered) scale and log measurements.

    Args:
        config: Loaded daemon configuration.
        once: If True, exit after recording a single measurement (or after
            ``once_timeout`` seconds without one) instead of running until a
            stop signal -- for an on-demand capture run by hand right
            before (or while) stepping on the scale, instead of a
            long-running service.
        once_timeout: Seconds to wait for one measurement before giving up.
            Only used when ``once`` is True.
        mqtt_config: Optional MQTT publishing configuration. If enabled,
            each measurement is also published as JSON. A broker outage is
            logged and non-fatal -- it never blocks local recording.
        profiles_config: Optional who-was-this tagging. If enabled, each
            measurement triggers a background notification (ntfy or
            dunstify, chosen based on ``api_config.enabled``) asking which
            profile it belongs to.
        api_config: Determines which profile-notification path is used.

    Returns:
        True if at least one measurement was recorded. Always True for a
        normal (non-``once``) run, which only returns via a stop signal.

    Raises:
        ConfigError: If the config names an unrecognized scale model.
    """
    address = config.address
    model_value = config.model

    if not address or not model_value:
        discovery_timeout = float(once_timeout) if once else 60.0
        address, model = await discover_scale(config.adapter or None, discovery_timeout)
        model_value = model.value
        persist_discovered_scale(config.config_path, address, model_value)
        _LOGGER.info(
            "Discovered %s at %s - saved to %s",
            model_value,
            address,
            config.config_path,
        )
    else:
        try:
            model = ScaleModel(model_value)
        except ValueError as exc:
            raise ConfigError(f"Unknown scale.model in config: {model_value!r}") from exc

    store = MeasurementStore(config.db_path)
    scanning_mode = (
        BluetoothScanningMode.PASSIVE
        if config.scanning_mode == "passive"
        else BluetoothScanningMode.ACTIVE
    )

    stop_event = asyncio.Event()
    measurement_received = False

    async with _mqtt_connection(mqtt_config) as mqtt_client:
        background_tasks: list[asyncio.Task] = []

        def on_measurement(data: ScaleData) -> None:
            nonlocal measurement_received
            row = _measurement_to_row(data, address, model_value)
            row_id = store.record(**row)
            measurement_received = True
            _LOGGER.info(
                "Recorded measurement from %s: weight=%s kg impedance=%s",
                address,
                row["weight_kg"],
                row["impedance_ohms"],
            )
            if mqtt_client is not None:
                background_tasks.append(
                    asyncio.create_task(
                        _publish_measurement(mqtt_client, mqtt_config, address, row)
                    )
                )
            if profiles_config.enabled:
                background_tasks.append(
                    asyncio.create_task(
                        _prompt_for_profile(
                            config.db_path,
                            row_id,
                            row["weight_kg"],
                            profiles_config,
                            api_config,
                        )
                    )
                )
            if once:
                stop_event.set()

        scale_kwargs: dict[str, object] = {"scanning_mode": scanning_mode}
        if config.adapter:
            scale_kwargs["adapter"] = config.adapter
        if model in _GATT_MODELS:
            scale_kwargs["cooldown_seconds"] = config.cooldown_seconds

        scale = SCALE_CLASSES[model](address, on_measurement, **scale_kwargs)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        _LOGGER.info(
            "Starting etekcity-scale-daemon %s for %s scale at %s%s",
            __version__,
            model_value,
            address,
            f" (once, {once_timeout}s timeout)" if once else "",
        )
        await scale.async_start()
        try:
            if once:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=once_timeout)
                except TimeoutError:
                    _LOGGER.warning(
                        "No measurement received within %s seconds", once_timeout
                    )
            else:
                await stop_event.wait()
        finally:
            _LOGGER.info("Shutting down")
            await scale.async_stop()
            if background_tasks:
                # A pending dunstify prompt can legitimately take up to its
                # configured timeout to resolve, and a retrying ntfy publish
                # can take up to its own worst-case retry budget; give
                # either that long instead of cutting it off at the same 5s
                # used for quick MQTT publishes, especially in --once mode
                # where the stop event fires as soon as the measurement is
                # recorded.
                if profiles_config.enabled and not api_config.enabled:
                    wait_timeout = profiles_config.dunstify_timeout_seconds + 5
                elif profiles_config.enabled and api_config.enabled:
                    wait_timeout = _NTFY_MAX_RETRY_SECONDS
                else:
                    wait_timeout = 5
                await asyncio.wait(background_tasks, timeout=wait_timeout)
            store.close()

    return measurement_received


def _check_config(config_path: str) -> int:
    """Validate a config file against every section loader, without running.

    Args:
        config_path: Path to the INI configuration file.

    Returns:
        0 if the file is valid (a summary is printed), 1 otherwise (each
        error is printed).
    """
    if not Path(config_path).is_file():
        print(f"Error: Config file not found: {config_path}")
        return 1

    errors: list[str] = []
    daemon_config = report_config = None
    mqtt_config = alert_config = api_config = profiles_config = None

    try:
        daemon_config = load_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        report_config = load_report_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        mqtt_config = load_mqtt_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        alert_config = load_alert_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        api_config = load_api_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        profiles_config = load_profiles_config(config_path)
    except ConfigError as exc:
        errors.append(str(exc))

    if (
        not errors
        and profiles_config.enabled
        and api_config.enabled
        and not profiles_config.ntfy_url
    ):
        errors.append(
            "profiles.enabled = yes with [api] enabled requires profiles.ntfy_url to be set"
        )

    biometrics_complete = 0
    orphaned_profiles: list[str] = []
    if not errors:
        for name in profiles_config.names:
            try:
                profile = load_profile_biometrics(config_path, name)
            except ConfigError as exc:
                errors.append(str(exc))
                continue
            if profile.skip_body_metrics or (
                profile.height_m and profile.birthdate and profile.sex
            ):
                biometrics_complete += 1

        tagged_profiles = get_distinct_profiles(daemon_config.db_path)
        orphaned_profiles = sorted(tagged_profiles - set(profiles_config.names))

    if errors:
        print(f"{config_path}: INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"{config_path}: OK")
    print(
        "  scale: address="
        f"{daemon_config.address or '(auto-discover)'} model="
        f"{daemon_config.model or '(auto-discover)'} adapter="
        f"{daemon_config.adapter or '(default)'}"
    )
    print(f"  storage: db_path={daemon_config.db_path}")
    print(f"  daemon: log_level={daemon_config.log_level}")
    print(
        "  report: layout="
        f"{report_config.layout} weight_unit={report_config.weight_unit} "
        f"date_format={report_config.date_format} page_size={report_config.page_size}"
    )
    print(
        "  mqtt: enabled="
        f"{'yes' if mqtt_config.enabled else 'no'} "
        f"host={mqtt_config.host or '(unset)'} port={mqtt_config.port}"
    )
    print(
        "  alerting: enabled="
        f"{'yes' if alert_config.enabled else 'no'} "
        f"stale_after_days={alert_config.stale_after_days} "
        f"weight_swing_threshold_kg={alert_config.weight_swing_threshold_kg} "
        f"urls={len(alert_config.apprise_urls)}"
    )
    print(
        "  api: enabled="
        f"{'yes' if api_config.enabled else 'no'} "
        f"host={api_config.host} port={api_config.port} "
        f"token={'(set)' if api_config.token else '(none)'}"
    )
    print(
        "  profiles: enabled="
        f"{'yes' if profiles_config.enabled else 'no'} "
        f"names={len(profiles_config.names)} "
        f"biometrics_complete={biometrics_complete}/{len(profiles_config.names)} "
        f"path={'ntfy' if api_config.enabled else 'dunstify'}"
    )
    if orphaned_profiles:
        print(
            "  warning: readings tagged with profile(s) not in profiles.names: "
            f"{', '.join(orphaned_profiles)} (still filterable via --profile, "
            "but there's no [profile.<name>] section for them, so biometrics/"
            "goal progress fall back to unset -- add them back to profiles.names "
            "or add a [profile.<name>] section if this wasn't intentional)"
        )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="etekcity-scale-daemon",
        description=(
            "Standalone BLE daemon that logs Etekcity smart scale "
            "measurements to a local SQLite database."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Path to the daemon's INI configuration file",
    )
    parser.add_argument(
        "-k",
        "--check-config",
        action="store_true",
        help="Validate the config file and exit, without starting the daemon",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (overrides the config file's log level)",
    )
    parser.add_argument(
        "-o",
        "--once",
        action="store_true",
        help=(
            "Record one measurement and exit, instead of running until "
            "stopped (run by hand right before stepping on the scale, "
            "instead of a long-running service)"
        ),
    )
    parser.add_argument(
        "-w",
        "--once-timeout",
        dest="once_timeout",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds to wait for a measurement in --once mode (default: %(default)s)",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)

    if args.check_config:
        return _check_config(args.config)

    try:
        config = load_config(args.config)
        mqtt_config = load_mqtt_config(args.config)
        profiles_config = load_profiles_config(args.config)
        api_config = load_api_config(args.config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error(str(exc))
        return 1

    if profiles_config.enabled and api_config.enabled and not profiles_config.ntfy_url:
        logging.basicConfig(level=logging.ERROR)
        _LOGGER.error(
            "profiles.enabled = yes with [api] enabled requires profiles.ntfy_url to be set"
        )
        return 1

    log_level = "DEBUG" if args.verbose else config.log_level
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        measurement_received = asyncio.run(
            run_daemon(
                config,
                once=args.once,
                once_timeout=args.once_timeout,
                mqtt_config=mqtt_config,
                profiles_config=profiles_config,
                api_config=api_config,
            )
        )
    except (TimeoutError, ConfigError) as exc:
        _LOGGER.error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 0

    if args.once and not measurement_received:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
