"""Serialización segura de valores en consultas Flux."""
import pandas as pd


def flux_string(value: object) -> str:
    """Devuelve un valor como literal de cadena Flux, con comillas y escapado.

    Un valor con comillas o barras invertidas no puede cerrar el literal ni
    alterar la consulta. Los saltos de línea y otros caracteres de control se
    rechazan porque ningún identificador legítimo los contiene.

    Args:
        value (object): Valor a insertar (bucket, measurement, CodeID, pie...).

    Returns:
        str: Literal Flux, por ejemplo ``"A\\"B"`` para ``A"B``.

    Raises:
        ValueError: Si el valor está vacío o contiene caracteres de control.
    """
    text = str(value)
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("Valor no admitido en una consulta Flux: vacío o con caracteres de control.")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def flux_range(start: str, stop: str) -> str:
    """Devuelve los argumentos de ``range()`` tras validar el intervalo.

    Args:
        start (str): Inicio en formato RFC 3339 (por ejemplo, el de ``influx_wallclock_z``).
        stop (str): Fin en formato RFC 3339.

    Returns:
        str: Texto ``start: <inicio>, stop: <fin>``.

    Raises:
        ValueError: Si alguna fecha no es interpretable o el inicio no es anterior al fin.
    """
    first, last = pd.Timestamp(start), pd.Timestamp(stop)
    if pd.isna(first) or pd.isna(last) or first >= last:
        raise ValueError("Rango temporal no válido: el inicio debe ser anterior al fin.")
    return f"start: {first.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}, stop: {last.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
