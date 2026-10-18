"""Persistencia verificada: un HDF5 por evento con publicación atómica, manifest y Excel atómico."""
import ctypes
import json
import logging
import os
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from msTools.i18n import _

from .registry import STATUS_OK, Trial

logger = logging.getLogger("msGait.Storage")

_META_ATTR = "trial_meta"
_EVENT_GLOB = "p_*/*/trial_*.h5"
_TEMPORARY_GLOB = "*.tmp-*"
# Motivo del archivo de eventos de un ensayo en el que el análisis no detecta nada.
NO_EVENTS = "sin eventos"


class StorageError(RuntimeError):
    """Fallo de escritura, verificación o bloqueo del almacén."""


def trial_key(trial: Trial, side: str) -> str:
    """Devuelve la clave lógica de un pie de un evento.

    Args:
        trial (Trial): Evento validado del registro.
        side (str): ``Left``, ``Right`` o ``Unknown``.

    Returns:
        str: Clave ``/p_{codeid}/{test}/trial_{id}/{pie}``, independiente del
        orden de las filas del registro.
    """
    return f"/p_{trial.codeid}/{trial.test_type}/trial_{trial.trial_id}/{side}"


def store_dir_for(h5_path: str | Path) -> Path:
    """Devuelve el directorio que contiene los HDF5 por evento.

    Args:
        h5_path (str | Path): Valor de ``paths.h5_path``.

    Returns:
        Path: Directorio con el mismo nombre que ``h5_path`` sin extensión.
    """
    return Path(h5_path).with_suffix("")


def manifest_path_for(h5_path: str | Path) -> Path:
    """Devuelve la ruta del manifest JSON Lines situado junto al almacén.

    Args:
        h5_path (str | Path): Valor de ``paths.h5_path``.

    Returns:
        Path: Ruta ``<nombre>.manifest.jsonl`` en el mismo directorio.
    """
    path = Path(h5_path)
    return path.with_name(f"{path.stem}.manifest.jsonl")


def _locate(h5_path: str | Path, key: str) -> tuple[Path, str]:
    """Traduce una clave lógica en el archivo del evento y el nodo del pie."""
    parts = key.strip("/").split("/")
    if len(parts) != 4:
        raise StorageError(_("Clave no válida: {0}").format(key))
    patient, test, event, side = parts
    return store_dir_for(h5_path) / patient / test / f"{event}.h5", side


def _file_sides(path: Path) -> list[str]:
    """Lista los pies guardados en el archivo de un evento."""
    with pd.HDFStore(str(path), mode="r") as store:
        return [node.strip("/") for node in store.keys()]


def list_keys(h5_path: str | Path) -> list[str]:
    """Lista las claves lógicas del almacén sin adquirir el bloqueo de escritura.

    Args:
        h5_path (str | Path): Valor de ``paths.h5_path``.

    Returns:
        list[str]: Claves existentes; lista vacía si el almacén no existe.
    """
    root = store_dir_for(h5_path)
    keys: list[str] = []
    if not root.is_dir():
        return keys
    for path in sorted(root.glob(_EVENT_GLOB)):
        prefix = "/" + path.relative_to(root).with_suffix("").as_posix()
        try:
            keys.extend(f"{prefix}/{side}" for side in _file_sides(path))
        except Exception as error:
            logger.warning(_("WARN_EVENT_FILE_UNREADABLE: {0} no se puede leer ({1}); se ignora").format(path, error))
    return keys


def _pid_alive(pid: int) -> bool:
    """Indica si existe un proceso con ese identificador."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # En Windows os.kill(pid, 0) envía una señal real; se consulta el proceso sin tocarlo.
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SignalStore:
    """Almacén de señales con un único escritor y un HDF5 por evento.

    Cada evento se escribe completo (todos sus pies) en un archivo temporal, se
    relee para verificarlo y solo entonces sustituye al archivo definitivo. Un
    corte del proceso deja intactos los eventos ya publicados.

    Se usa como gestor de contexto: al entrar adquiere un archivo de bloqueo
    ``<h5_path>.lock`` y al salir lo libera.

    Attributes:
        path (Path): Valor de ``paths.h5_path`` (da nombre al almacén).
        root (Path): Directorio con los archivos ``p_{codeid}/{test}/trial_{id}.h5``.
        lock_path (Path): Ruta del archivo de bloqueo.
    """

    def __init__(self, h5_path: str | Path) -> None:
        """Prepara el almacén sin abrir ni bloquear nada.

        Args:
            h5_path (str | Path): Valor de ``paths.h5_path``.
        """
        self.path = Path(h5_path)
        self.root = store_dir_for(self.path)
        self.lock_path = Path(f"{self.path}.lock")
        self._locked = False

    def _acquire_lock(self) -> None:
        """Crea el archivo de bloqueo, retirando antes uno huérfano si lo hay.

        Raises:
            StorageError: Si el proceso que creó el bloqueo sigue en ejecución o
                el bloqueo no identifica a ningún proceso.
        """
        for _attempt in range(2):
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    owner = int(self.lock_path.read_text().strip())
                except (OSError, ValueError):
                    owner = None
                if owner is None or _pid_alive(owner):
                    raise StorageError(
                        _("El almacén está bloqueado por otra ejecución ({0}, proceso {1}).").format(self.lock_path, owner)
                    ) from None
                logger.warning(
                    _("WARN_STALE_LOCK: Bloqueo huérfano del proceso {0}, que ya no existe; se retira {1}").format(owner, self.lock_path)
                )
                self.lock_path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w") as lock:
                lock.write(str(os.getpid()))
            self._locked = True
            return
        raise StorageError(_("No se pudo adquirir el bloqueo {0}.").format(self.lock_path))

    def __enter__(self) -> "SignalStore":
        """Adquiere el bloqueo de escritor y limpia restos de una ejecución cortada.

        Returns:
            SignalStore: El propio almacén, listo para escribir.

        Raises:
            StorageError: Si otro proceso en ejecución tiene el bloqueo.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        for leftover in self.root.rglob(_TEMPORARY_GLOB):
            logger.warning(_("WARN_TEMPORARY_REMOVED: Se elimina el temporal de una ejecución interrumpida: {0}").format(leftover))
            leftover.unlink(missing_ok=True)
        if self.path.is_file():
            logger.warning(
                _("WARN_LEGACY_STORE: {0} es un almacén del formato anterior (un solo archivo); no se usa. Los eventos se guardan en {1}").format(self.path, self.root)
            )
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Libera el bloqueo de escritor."""
        if self._locked:
            self.lock_path.unlink(missing_ok=True)
            self._locked = False

    def has(self, key: str) -> bool:
        """Indica si una clave existe en el almacén.

        Args:
            key (str): Clave lógica ``/p_{codeid}/{test}/trial_{id}/{pie}``.

        Returns:
            bool: ``True`` si el archivo del evento existe y contiene ese pie.
        """
        path, side = _locate(self.path, key)
        try:
            return path.is_file() and side in _file_sides(path)
        except Exception:
            return False

    def write_event(self, trial: Trial, series: dict[str, tuple[pd.DataFrame, dict[str, Any]]]) -> dict[str, str]:
        """Publica de forma atómica todas las series de un evento.

        Escribe un temporal junto al destino, lo relee y comprueba filas,
        instantes, columnas y atributos de cada pie, y solo entonces reemplaza
        el archivo del evento. Si algo falla, el archivo anterior (si existía)
        no cambia.

        Args:
            trial (Trial): Evento al que pertenecen las series.
            series (dict[str, tuple[pd.DataFrame, dict[str, Any]]]): Por pie, la
                serie normalizada y sus atributos (id, fechas, paciente, test,
                pie, dispositivo).

        Returns:
            dict[str, str]: Clave lógica publicada por pie.

        Raises:
            StorageError: Si no se tiene el bloqueo, o falla la escritura, la
                verificación o el reemplazo.
        """
        if not self._locked:
            raise StorageError(_("El almacén debe abrirse con 'with' antes de escribir."))
        keys = {side: trial_key(trial, side) for side in series}
        if not keys:
            return keys
        target, _side = _locate(self.path, next(iter(keys.values())))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # nombres con guiones: aviso NaturalName de PyTables
                with pd.HDFStore(str(temporary), mode="w") as store:
                    for side, (frame, meta) in series.items():
                        export = frame.copy()
                        for column in export.columns:
                            if not pd.api.types.is_numeric_dtype(export[column]) and not pd.api.types.is_datetime64_any_dtype(export[column]):
                                export[column] = export[column].astype(str).astype(object)
                        store.put(side, export, format="table")
                        setattr(store.get_storer(side).attrs, _META_ATTR, meta)
                with pd.HDFStore(str(temporary), mode="r") as store:
                    for side, (frame, meta) in series.items():
                        stored = store.get(side)
                        stored_meta = getattr(store.get_storer(side).attrs, _META_ATTR, None)
                        same_times = len(stored) == len(frame) and bool(
                            (stored["_time"].to_numpy() == frame["_time"].to_numpy()).all()
                        )
                        if not same_times or list(stored.columns) != list(frame.columns) or stored_meta != meta:
                            raise StorageError(
                                _("Verificación fallida en {0}: {1} filas leídas de {2} enviadas").format(keys[side], len(stored), len(frame))
                            )
            os.replace(temporary, target)
        except StorageError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise StorageError(_("No se pudo escribir el evento {0}: {1}").format(trial.trial_id, error)) from error
        logger.debug(_("DEBUG_HDF5_VERIFIED: {0} verificado y publicado ({1})").format(target, ', '.join(series)))
        return keys

    def read(self, key: str) -> pd.DataFrame:
        """Lee una serie almacenada.

        Args:
            key (str): Clave lógica.

        Returns:
            pd.DataFrame: Serie almacenada.
        """
        path, side = _locate(self.path, key)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pd.HDFStore(str(path), mode="r") as store:
                return store.get(side)

    def meta(self, key: str) -> dict[str, Any]:
        """Lee los atributos de evento guardados junto a una clave.

        Args:
            key (str): Clave lógica.

        Returns:
            dict[str, Any]: Atributos del evento; vacío si no existen.
        """
        return read_meta(self.path, key) or {}


def read_meta(h5_path: str | Path, key: str) -> dict[str, Any] | None:
    """Lee los atributos de evento guardados junto a una clave.

    Args:
        h5_path (str | Path): Valor de ``paths.h5_path``.
        key (str): Clave lógica.

    Returns:
        dict[str, Any] | None: Atributos del evento, o ``None`` si no existen.
    """
    path, side = _locate(h5_path, key)
    with pd.HDFStore(str(path), mode="r") as store:
        return getattr(store.get_storer(side).attrs, _META_ATTR, None)


def events_path_for(results_folder: str | Path, trial: Trial) -> Path:
    """Devuelve el archivo de eventos detectados de un ensayo.

    Args:
        results_folder (str | Path): Valor de ``paths.results_folder``, una
            carpeta de resultados separada de los datos en bruto.
        trial (Trial): Evento del registro.

    Returns:
        Path: ``<results_folder>/eventos/p_{codeid}/{test}/trial_{id}.h5``.
    """
    return Path(results_folder) / "eventos" / f"p_{trial.codeid}" / trial.test_type / f"trial_{trial.trial_id}.h5"


def write_derived(results_folder: str | Path, trial: Trial, tables: dict[str, pd.DataFrame], meta: dict[str, Any]) -> Path | None:
    """Guarda de forma atómica lo detectado en un ensayo, para poder reconstruir sus métricas.

    Escribe un archivo por ensayo (:func:`events_path_for`) con una tabla de
    eventos por pie (``{pie}/eventos``: contactos de talón, despegues, giros y
    tramos excluidos con su motivo) y otra con las zancadas integradas
    (``{pie}/zancadas``). Como atributos lleva la versión del código, la huella
    de la configuración y la fecha de la ejecución. Cada ejecución sustituye el
    archivo del ensayo, también cuando no detecta nada: entonces el archivo
    solo lleva una tabla ``resumen`` con el motivo ``sin eventos`` y los
    atributos de la ejecución, para que no quede el resultado de una
    configuración anterior.

    Args:
        results_folder (str | Path): Valor de ``paths.results_folder``.
        trial (Trial): Evento al que pertenecen las tablas.
        tables (dict[str, pd.DataFrame]): Tabla por clave HDF5; las vacías se omiten.
        meta (dict[str, Any]): Atributos (versión, huella de configuración, fechas).

    Returns:
        Path: Archivo publicado.

    Raises:
        StorageError: Si falla la escritura o el reemplazo.
    """
    tables = {name: table for name, table in tables.items() if table is not None and not table.empty}
    if not tables:
        tables = {"resumen": pd.DataFrame({"motivo": [NO_EVENTS]})}
    target = events_path_for(results_folder, trial)
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pd.HDFStore(str(temporary), mode="w") as store:
                for name, table in tables.items():
                    export = table.copy()
                    for column in export.columns:
                        if not pd.api.types.is_numeric_dtype(export[column]) and not pd.api.types.is_datetime64_any_dtype(export[column]):
                            export[column] = export[column].astype(str).astype(object)
                    store.put(name, export, format="table")
                    setattr(store.get_storer(name).attrs, _META_ATTR, meta)
        os.replace(temporary, target)
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise StorageError(_("No se pudo escribir el evento {0}: {1}").format(trial.trial_id, error)) from error
    logger.debug(_("DEBUG_DERIVED_SAVED: {0} guardado ({1})").format(target, ", ".join(tables)))
    return target


class Manifest:
    """Registro JSON Lines de eventos y ejecuciones, situado junto al almacén.

    Cada línea es un objeto con ``record`` igual a ``run_start``, ``event`` o
    ``run_end``. Una ejecución sin ``run_end`` quedó incompleta.

    Attributes:
        path (Path): Ruta del archivo manifest.
    """

    def __init__(self, path: str | Path) -> None:
        """Asocia el manifest a un archivo, que se crea al añadir la primera línea.

        Args:
            path (str | Path): Ruta del archivo JSON Lines.
        """
        self.path = Path(path)

    def append(self, record: dict[str, Any]) -> None:
        """Añade una línea y fuerza su escritura en disco.

        Args:
            record (dict[str, Any]): Objeto serializable a JSON.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def records(self) -> list[dict[str, Any]]:
        """Lee todas las líneas válidas del manifest.

        Una línea truncada (corte durante la escritura) se ignora con aviso.

        Returns:
            list[dict[str, Any]]: Líneas en orden de escritura.
        """
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(_("WARN_MANIFEST_LINE: Línea {0} ilegible en {1}; se ignora").format(number, self.path))
        return records

    def completed_keys(self) -> dict[str, list[str]]:
        """Devuelve los eventos cuyo último estado registrado es ``OK``.

        Returns:
            dict[str, list[str]]: Claves lógicas escritas, por identificador de evento.
        """
        last: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if record.get("record") == "event":
                last[str(record.get("trial_id"))] = record
        return {
            trial_id: [foot["key"] for foot in record.get("feet", {}).values() if "key" in foot]
            for trial_id, record in last.items()
            if record.get("status") == STATUS_OK
        }


def audit_store(h5_path: str | Path, trials: Iterable[Trial], manifest: Manifest) -> dict[str, list[str]]:
    """Compara las claves del almacén con el registro y con el manifest.

    Args:
        h5_path (str | Path): Valor de ``paths.h5_path``.
        trials (Iterable[Trial]): Eventos válidos del registro.
        manifest (Manifest): Manifest asociado al almacén.

    Returns:
        dict[str, list[str]]: ``missing`` (eventos del registro sin ninguna clave),
        ``orphan`` (claves que no corresponden a ningún evento del registro),
        ``legacy`` (almacén del formato anterior, de un solo archivo, si existe)
        y ``unconfirmed`` (eventos con claves pero sin estado ``OK`` en el manifest).
    """
    keys = list_keys(h5_path)
    completed = manifest.completed_keys()
    missing, unconfirmed, expected = [], [], set()
    for trial in trials:
        prefix = trial_key(trial, "")
        found = [key for key in keys if key.startswith(prefix)]
        expected.update(found)
        if not found:
            missing.append(trial.trial_id)
        elif trial.trial_id not in completed:
            unconfirmed.append(trial.trial_id)
    return {
        "missing": missing, "orphan": sorted(set(keys) - expected),
        "legacy": [str(h5_path)] if Path(h5_path).is_file() else [], "unconfirmed": unconfirmed,
    }


def set_aside(output: str | Path) -> Path | None:
    """Aparta un Excel de resultados para que no parezca de la ejecución en curso.

    Lo renombra en la misma carpeta como ``{nombre}.anterior-AAAAMMDD-HHMMSS{ext}``,
    con la fecha de su última modificación, sin borrar nada.

    Args:
        output (str | Path): Ruta del Excel (``paths.output_dataset``).

    Returns:
        Path | None: Nueva ruta del archivo apartado, o ``None`` si no existía.

    Raises:
        StorageError: Si no se puede renombrar (por ejemplo, abierto en Excel).
    """
    output = Path(output).resolve()
    if not output.is_file():
        return None
    stamp = datetime.fromtimestamp(output.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    target = output.with_name(f"{output.stem}.anterior-{stamp}{output.suffix}")
    counter = 1
    while target.exists():
        counter += 1
        target = output.with_name(f"{output.stem}.anterior-{stamp}-{counter}{output.suffix}")
    try:
        os.replace(output, target)
    except OSError as error:
        raise StorageError(_("No se pudo apartar {0}: {1}").format(output, error)) from error
    logger.warning(_("WARN_EXCEL_SET_ASIDE: El Excel anterior se ha apartado como {0}").format(target))
    return target


def publish_excel(sheets: dict[str, pd.DataFrame], output: str | Path, index_sheets: Iterable[str] = ()) -> Path:
    """Publica un Excel de forma atómica: temporal, verificación y reemplazo.

    El archivo de destino solo cambia si el temporal se relee con las mismas
    hojas y número de filas.

    Args:
        sheets (dict[str, pd.DataFrame]): Tabla por nombre de hoja.
        output (str | Path): Ruta final del Excel.
        index_sheets (Iterable[str]): Hojas cuyo índice debe escribirse.

    Returns:
        Path: Ruta final publicada.

    Raises:
        StorageError: Si falla la escritura, la verificación o el reemplazo
            (por ejemplo, con el archivo de destino abierto en Excel).
    """
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.tmp-{os.getpid()}{output.suffix}")
    with_index = set(index_sheets)
    try:
        with pd.ExcelWriter(temporary) as writer:
            for name, table in sheets.items():
                table.to_excel(writer, sheet_name=name, index=name in with_index)
        written = pd.read_excel(temporary, sheet_name=None)
        for name, table in sheets.items():
            if name not in written or len(written[name]) != len(table):
                raise StorageError(_("Verificación fallida en la hoja '{0}' de {1}").format(name, temporary.name))
        os.replace(temporary, output)
    except StorageError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as error:
        temporary.unlink(missing_ok=True)
        raise StorageError(_("No se pudo publicar {0}: {1}").format(output, error)) from error
    logger.debug(_("DEBUG_EXCEL_PUBLISHED: {0} verificado y publicado ({1} hojas)").format(output, len(sheets)))
    return output
