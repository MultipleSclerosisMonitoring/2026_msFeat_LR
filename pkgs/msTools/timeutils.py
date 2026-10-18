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


# Criterios para pasar la hora del registro a la escala de InfluxDB (registry.influx_time).
TIME_CRITERIA = ("civil", "utc")


def influx_time(ts, criterion="civil"):
    """Pasa una hora del registro a la escala en la que se busca en InfluxDB.

    - ``civil``: hora de reloj tal como figura en el registro, sin zona
      (:func:`experiment_wallclock`). Es el criterio acordado en R27.
    - ``utc``: instante real convertido a UTC (una hora antes en invierno y dos
      en verano). Una hora sin zona se interpreta como hora de Madrid.

    Args:
        ts: Hora del registro (texto, ``datetime`` o ``Timestamp``).
        criterion (str): ``civil`` o ``utc``.

    Returns:
        pd.Timestamp: Hora sin zona en la escala de InfluxDB.

    Raises:
        ValueError: Si el criterio no es uno de ``TIME_CRITERIA``.
    """
    if criterion == "civil":
        return experiment_wallclock(ts)
    if criterion == "utc":
        return ensure_utc(ts).tz_localize(None)
    raise ValueError(f"Criterio horario desconocido: {criterion!r} (admitidos: {', '.join(TIME_CRITERIA)})")


def criterion_for(ts, setting="civil"):
    """Elige el criterio horario de una hora del registro según su fecha.

    Args:
        ts: Hora del registro.
        setting: ``civil``, ``utc`` o una lista de periodos ``{"desde": fecha,
            "criterio": civil|utc}`` en orden. Cada periodo rige desde su fecha
            (hora civil del registro) hasta el siguiente; el primero puede no
            tener ``desde`` y rige desde el principio.

    Returns:
        str: ``civil`` o ``utc``.

    Raises:
        ValueError: Si la fecha es anterior a todos los periodos definidos.
    """
    if isinstance(setting, str):
        return setting
    moment = experiment_wallclock(ts)
    chosen = None
    for period in setting:
        start = period.get("desde")
        if start is None or moment >= pd.Timestamp(start):
            chosen = period["criterio"]
    if chosen is None:
        raise ValueError(f"Ningún periodo de registry.influx_time cubre {moment}")
    return chosen


def influx_time_z(ts, criterion="civil"):
    """Formatea una hora del registro para ``range()`` de Flux según el criterio.

    Args:
        ts: Hora del registro.
        criterion (str): ``civil`` o ``utc`` (:func:`influx_time`).

    Returns:
        str: Hora en formato ``AAAA-MM-DDTHH:MM:SS[.ffffff]Z``.
    """
    text = influx_time(ts, criterion).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
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
