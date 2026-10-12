import pandas as pd


def experiment_wallclock(ts):
    """
    Return the timestamp exactly as written in the experiment tables.

    The historical InfluxDB data stores Europe/Madrid wall-clock times as if
    they were UTC. For Influx queries we must therefore preserve the displayed
    clock time and drop any timezone offset instead of converting to UTC.

    The trial registry stores real instants with their offset (``+01`` in
    winter, ``+02`` in summer): ``2025-12-12 19:24:00+01`` is 18:24 UTC, and
    this function returns the wall-clock ``19:24``. Everything verifiable is
    coherent with this contract from November 2025 onwards.
    """
    ts = pd.to_datetime(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def influx_wallclock_z(ts):
    """
    Format an experiment-table timestamp for Flux range() without UTC shifting.

    Example: ``2025-12-12 11:34:00+01`` becomes
    ``2025-12-12T11:34:00Z``.
    """
    ts = experiment_wallclock(ts)
    text = ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return text.replace('.000000Z', 'Z')


def ensure_utc(ts):
    """
    Convert a datetime/string/Timestamp to a pandas Timestamp with UTC timezone.

    - If the input is timezone-naive, **assumes Europe/Madrid** before converting.
    - If the input already has any timezone, simply converts it to UTC.

    :param ts: datetime, pd.Timestamp, or str in ISO (or similar) format.
    :return: pd.Timestamp with tzinfo set to UTC.
    """
    ts = pd.to_datetime(ts)
    if ts.tzinfo is None:
        # Localize naive timestamps to Europe/Madrid
        ts = ts.tz_localize("Europe/Madrid") #  ,
    #                       ambiguous='infer',nonexistent='shift_forward')
    # Convert any timezone-aware timestamp to UTC
    return ts.tz_convert("UTC")
