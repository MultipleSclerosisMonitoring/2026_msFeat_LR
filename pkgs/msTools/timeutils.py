import pandas as pd

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
