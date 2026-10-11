import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs"))

import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Mapping

import numpy as np
import pandas as pd
from scipy.integrate import cumulative_trapezoid
from scipy.signal import butter, detrend, filtfilt, find_peaks

from msConfig import ConfigError
from msTools.i18n import _

# Inicializamos el logger estructurado para este módulo
logger = logging.getLogger("msGait.GaitMetricsCalculator")

SIGNAL_CHANNELS = ("Ax", "Ay", "Az", "Gx", "Gy", "Gz", "S0", "S1", "S2")
# Métricas por pie que forman las tablas y estadísticas del informe (lista explícita).
REPORT_METRICS = ("pasos", "cadencia", "apoyo_prc", "balanceo_prc", "decaimiento_prc", "stride_length_cm", "step_height_cm")
SYMMETRY_METRIC = "symmetry_index"
PRESSURE_CHANNELS = ("S0", "S1", "S2")
STEP_MODALITIES = ("auto", "accel", "pressure")


@dataclass(frozen=True)
class ProcessingConfig:
    """Parámetros del procesador de señales (sección ``processing`` del YAML).

    Todas las ventanas y distancias se expresan en segundos y se convierten a
    muestras con la frecuencia efectiva medida en cada serie.

    Attributes:
        fs (float): Frecuencia de muestreo nominal en Hz. Solo se usa para
            avisar si la frecuencia efectiva medida se aparta de ella.
        fs_tolerance (float): Desviación relativa admitida entre la frecuencia
            efectiva y la nominal antes de marcar la serie.
        max_gap_s (float): Un intervalo entre muestras mayor que este valor se
            trata como hueco y corta la serie en segmentos.
        max_interpolation_s (float): Duración máxima de un tramo sin valor que
            se interpola linealmente; los más largos cortan el segmento.
        min_coverage (float): Fracción mínima de muestras válidas de un canal.
        filter_order (int): Orden del filtro Butterworth.
        accel_cutoff_hz (float): Corte del pasabajos de aceleración.
        gyro_cutoff_hz (float): Corte del pasabajos del giroscopio.
        pressure_cutoff_hz (float): Corte del pasabajos de la presión del talón.
        pressure_window_s (float): Ventana de suavizado de la presión total.
        turn_axis (str): Canal del giroscopio usado para detectar giros.
        turn_threshold (float): Umbral absoluto de ese canal para marcar giro.
        turn_min_duration_s (float): Duración mínima de un episodio por encima
            del umbral para contarlo como giro; los más cortos (por ejemplo, el
            balanceo del pie) se ignoran y se cuentan aparte. 0 no filtra.
        min_steady_s (float): Marcha estable mínima tras retirar los giros.
        step_min_distance_s (float): Separación mínima entre picos de aceleración.
        step_min_threshold (float): Altura mínima de un pico de aceleración.
        step_std_factor (float): Factor de la desviación típica en el umbral dinámico.
        step_prominence (float | None): Prominencia mínima del pico; ``None`` no la exige.
        step_modality (str): ``auto``, ``accel`` o ``pressure``.
        rescue_min_steps (int): En ``auto``, si la aceleración detecta menos
            picos que este número y la presión detecta más, se usa la presión.
        pressure_step_distance_s (float): Separación mínima entre picos de presión.
        pressure_step_height (float): Altura mínima de un pico de presión del talón.
        cadence_min (float): Límite inferior de cadencia plausible.
        cadence_max (float): Límite superior de cadencia plausible.
        pressure_stance_is_low (bool): Polaridad: ``True`` si la presión baja indica apoyo.
        cycle_min_s (float): Duración mínima de un ciclo de marcha válido.
        cycle_max_s (float): Duración máxima de un ciclo de marcha válido.
        decay_block_s (float): Duración de los bloques inicial y final del decaimiento.
        decay_min_duration_s (float): Duración mínima del ensayo para calcular decaimiento.
        stride_window_s (float): Ventana del rango pico a pico de aceleración.
        stride_k (float): Constante del estimador de longitud (no calibrada).
        stride_exponent (float): Exponente del estimador de longitud.
        stride_clip_min_cm (float): Recorte inferior del valor informado.
        stride_clip_max_cm (float): Recorte superior del valor informado.
        stride_ok_min_cm (float): Límite inferior de longitud plausible.
        stride_ok_max_cm (float): Límite superior de longitud plausible.
        swing_min_s (float): Duración mínima de un balanceo válido.
        swing_max_s (float): Duración máxima de un balanceo válido.
        height_ok_min_cm (float): Límite inferior de altura plausible.
        height_ok_max_cm (float): Límite superior de altura plausible.
        gravity (float): Aceleración de la gravedad para pasar de g a m/s².
    """
    fs: float = 100.0
    fs_tolerance: float = 0.10
    max_gap_s: float = 0.5
    max_interpolation_s: float = 0.1
    min_coverage: float = 0.8
    filter_order: int = 4
    accel_cutoff_hz: float = 3.0
    gyro_cutoff_hz: float = 5.0
    pressure_cutoff_hz: float = 5.0
    pressure_window_s: float = 0.15
    turn_axis: str = "Gz"
    turn_threshold: float = 50.0
    turn_min_duration_s: float = 1.0
    min_steady_s: float = 3.0
    step_min_distance_s: float = 0.4
    step_min_threshold: float = 0.15
    step_std_factor: float = 0.3
    step_prominence: float | None = None
    step_modality: str = "auto"
    rescue_min_steps: int = 5
    pressure_step_distance_s: float = 0.5
    pressure_step_height: float = 200.0
    cadence_min: float = 30.0
    cadence_max: float = 200.0
    pressure_stance_is_low: bool = True
    cycle_min_s: float = 0.5
    cycle_max_s: float = 3.0
    decay_block_s: float = 60.0
    decay_min_duration_s: float = 120.0
    stride_window_s: float = 2.0
    stride_k: float = 50.0
    stride_exponent: float = 0.25
    stride_clip_min_cm: float = 20.0
    stride_clip_max_cm: float = 160.0
    stride_ok_min_cm: float = 30.0
    stride_ok_max_cm: float = 150.0
    swing_min_s: float = 0.2
    swing_max_s: float = 1.5
    height_ok_min_cm: float = 2.0
    height_ok_max_cm: float = 25.0
    gravity: float = 9.81

    def __post_init__(self) -> None:
        """Valida tipos, signos y límites cruzados.

        Raises:
            ConfigError: Si algún parámetro no es válido o dos límites se contradicen.
        """
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in ("turn_axis", "step_modality", "pressure_stance_is_low"):
                continue
            if item.name == "step_prominence" and value is None:
                continue
            if item.name == "turn_min_duration_s" and value == 0 and not isinstance(value, bool):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ConfigError(_("processing.{0} debe ser un número finito mayor que cero.").format(item.name))
        for name in ("filter_order", "rescue_min_steps"):
            if not isinstance(getattr(self, name), int):
                raise ConfigError(_("processing.{0} debe ser un entero.").format(name))
        if not isinstance(self.pressure_stance_is_low, bool):
            raise ConfigError(_("processing.pressure_stance_is_low debe ser true o false."))
        if self.turn_axis not in SIGNAL_CHANNELS:
            raise ConfigError(_("processing.turn_axis debe ser uno de {0}.").format(', '.join(SIGNAL_CHANNELS)))
        if self.step_modality not in STEP_MODALITIES:
            raise ConfigError(_("processing.step_modality debe ser uno de {0}.").format(', '.join(STEP_MODALITIES)))
        if self.max_gap_s <= 1 / self.fs:
            raise ConfigError(_("processing.max_gap_s debe ser mayor que el intervalo nominal entre muestras."))
        if self.min_coverage > 1:
            raise ConfigError(_("processing.min_coverage debe estar entre 0 y 1."))
        nyquist = self.fs / 2
        for name in ("accel_cutoff_hz", "gyro_cutoff_hz", "pressure_cutoff_hz"):
            if getattr(self, name) >= nyquist:
                raise ConfigError(_("processing.{0} debe ser menor que la frecuencia de Nyquist ({1} Hz).").format(name, nyquist))
        for low, high in (
            ("cadence_min", "cadence_max"), ("cycle_min_s", "cycle_max_s"), ("swing_min_s", "swing_max_s"),
            ("stride_clip_min_cm", "stride_clip_max_cm"), ("stride_ok_min_cm", "stride_ok_max_cm"),
            ("height_ok_min_cm", "height_ok_max_cm"),
        ):
            if getattr(self, low) >= getattr(self, high):
                raise ConfigError(_("processing.{0} debe ser menor que processing.{1}.").format(low, high))
        if self.decay_min_duration_s < 2 * self.decay_block_s:
            raise ConfigError(_("processing.decay_min_duration_s debe ser al menos el doble de processing.decay_block_s."))

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any] | None) -> "ProcessingConfig":
        """Construye la configuración a partir de la sección ``processing``.

        Args:
            section (Mapping[str, Any] | None): Claves del YAML; las ausentes
                toman el valor por defecto.

        Returns:
            ProcessingConfig: Configuración validada.

        Raises:
            ConfigError: Si hay claves desconocidas o valores no válidos.
        """
        section = dict(section or {})
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(section) - known)
        if unknown:
            raise ConfigError(_("Claves desconocidas en processing: {0}.").format(', '.join(unknown)))
        for name in ("fs", "fs_tolerance", "max_gap_s", "turn_min_duration_s"):
            if isinstance(section.get(name), int) and not isinstance(section[name], bool):
                section[name] = float(section[name])
        return cls(**section)

    def to_record(self) -> Dict[str, Any]:
        """Devuelve los parámetros usados, para exportarlos junto a los resultados.

        Returns:
            Dict[str, Any]: Valor por nombre de parámetro.
        """
        return asdict(self)


@dataclass
class PreparedSignal:
    """Serie de un pie validada en tiempo y lista para calcular métricas.

    Attributes:
        t (np.ndarray): Segundos desde la primera muestra, estrictamente crecientes.
        data (pd.DataFrame): Canales numéricos, con los tramos cortos interpolados.
        segment (np.ndarray): Identificador de segmento continuo de cada muestra.
        dt (float): Intervalo medio entre muestras dentro de los segmentos, en segundos.
        fs (float): Frecuencia efectiva: muestras por segundo de señal continua.
        report (Dict[str, Any]): Calidad temporal y cobertura por canal.
        error (str): Motivo por el que la serie no puede usarse; vacío si es válida.
    """
    t: np.ndarray = field(default_factory=lambda: np.array([]))
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    segment: np.ndarray = field(default_factory=lambda: np.array([]))
    dt: float = float("nan")
    fs: float = float("nan")
    report: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Devuelve los tramos ``[inicio, fin)`` consecutivos en los que la máscara es cierta."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


class GaitMetricsCalculator:
    """Clase encargada del procesamiento matemático de señales de sensores inerciales
    para la extracción de métricas espacio-temporales de la marcha.

    Todas las duraciones se miden con ``_time``. Las métricas que no pueden
    calcularse devuelven ``NaN`` con ``status`` y ``error_reason``, nunca cero.

    Attributes:
        config (ProcessingConfig): Parámetros usados en todos los métodos.
    """

    def __init__(self, config: ProcessingConfig | None = None) -> None:
        """Crea el calculador con una configuración de procesado.

        Args:
            config (ProcessingConfig | None): Parámetros; ``None`` usa los valores por defecto.
        """
        self.config = config or ProcessingConfig()

    # ------------------------------------------------------------------ utilidades

    @staticmethod
    def seconds_to_samples(seconds: float, fs: float, minimum: int = 1) -> int:
        """Convierte una duración en un número entero de muestras.

        Política única: redondeo al entero más cercano, con un mínimo.

        Args:
            seconds (float): Duración en segundos.
            fs (float): Frecuencia de muestreo en Hz.
            minimum (int): Número mínimo de muestras devuelto.

        Returns:
            int: Número de muestras.
        """
        return max(int(minimum), int(round(float(seconds) * float(fs))))

    def _lowpass(self, values: np.ndarray, cutoff: float, fs: float) -> tuple[np.ndarray, str]:
        """Filtra un tramo continuo y devuelve también el método aplicado.

        Args:
            values (np.ndarray): Muestras de un tramo continuo y sin valores ausentes.
            cutoff (float): Frecuencia de corte en Hz.
            fs (float): Frecuencia efectiva en Hz.

        Returns:
            tuple[np.ndarray, str]: Señal filtrada y ``butterworth`` o ``media_movil``
            (tramo demasiado corto para el filtro de fase cero).
        """
        order = self.config.filter_order
        b, a = butter(order, cutoff / (0.5 * fs), btype="low", analog=False)
        if len(values) <= 3 * max(len(a), len(b)):
            window = min(len(values), max(3, order * 2 + 1))
            smooth = pd.Series(values).rolling(window=window, center=True, min_periods=1).mean().to_numpy()
            return smooth, "media_movil"
        return filtfilt(b, a, values), "butterworth"

    def _valid_runs(self, prepared: PreparedSignal, channels: tuple[str, ...], extra: np.ndarray | None = None) -> list[tuple[int, int]]:
        """Devuelve los tramos continuos en tiempo con todos los canales válidos.

        Args:
            prepared (PreparedSignal): Serie preparada.
            channels (tuple[str, ...]): Canales que deben tener valor.
            extra (np.ndarray | None): Máscara adicional (por ejemplo, sin giros).

        Returns:
            list[tuple[int, int]]: Tramos ``[inicio, fin)`` que no cruzan huecos.
        """
        valid = np.ones(len(prepared.t), dtype=bool)
        for channel in channels:
            valid &= prepared.data[channel].notna().to_numpy()
        if extra is not None:
            valid &= extra
        runs: list[tuple[int, int]] = []
        for start, stop in _true_runs(valid):
            cuts = np.flatnonzero(np.diff(prepared.segment[start:stop]) != 0) + start + 1
            limits = [start, *cuts, stop]
            runs.extend(zip(limits[:-1], limits[1:]))
        return runs

    def _exposure(self, prepared: PreparedSignal, runs: list[tuple[int, int]]) -> float:
        """Tiempo real cubierto por unos tramos, en segundos."""
        return float(sum(prepared.t[stop - 1] - prepared.t[start] + prepared.dt for start, stop in runs))

    def _filtered(self, prepared: PreparedSignal, channel: str, cutoff: float) -> tuple[np.ndarray, set[str]]:
        """Filtra un canal tramo a tramo, sin mezclar muestras separadas por huecos."""
        out = np.full(len(prepared.t), np.nan)
        methods: set[str] = set()
        values = prepared.data[channel].to_numpy(dtype=float)
        for start, stop in self._valid_runs(prepared, (channel,)):
            out[start:stop], method = self._lowpass(values[start:stop], cutoff, prepared.fs)
            methods.add(method)
        return out, methods

    def _failure(self, keys: tuple[str, ...], status: str, reason: str, prepared: PreparedSignal | None = None, **extra: Any) -> Dict[str, Any]:
        """Construye un resultado sin valor (``NaN``) con su estado y motivo."""
        result: Dict[str, Any] = {key: float("nan") for key in keys}
        result.update({"status": status, "error_reason": reason})
        if prepared is not None:
            result.update(self._quality(prepared))
        result.update(extra)
        return result

    @staticmethod
    def _quality(prepared: PreparedSignal) -> Dict[str, Any]:
        """Campos de calidad temporal comunes a todos los resultados."""
        return {
            "fs_efectiva": prepared.report.get("fs_efectiva", float("nan")),
            "duracion_real_seg": prepared.report.get("duracion_real_seg", float("nan")),
            "n_huecos": prepared.report.get("n_huecos", 0),
        }

    # ------------------------------------------------------------------ preparación

    def preparar(self, df: pd.DataFrame) -> PreparedSignal:
        """Valida el tiempo, mide la frecuencia efectiva y trata los huecos.

        Exige la columna ``_time``, ordena si hace falta, corta la serie en
        segmentos continuos donde el intervalo supera ``max_gap_s`` y convierte
        los canales a numérico. La frecuencia efectiva es el número de muestras
        por segundo de señal continua (tasa media): es robusta frente a marcas
        de tiempo que llegan en ráfagas, que se informan como irregularidad.
        Los valores ausentes solo se interpolan en tramos de hasta
        ``max_interpolation_s``; el resto queda como ausente y corta el
        segmento. Nada se sustituye por cero.

        Args:
            df (pd.DataFrame): Serie de un pie con ``_time`` y los canales de señal.

        Returns:
            PreparedSignal: Serie preparada. Si no es utilizable, ``error``
            indica el motivo.
        """
        cfg = self.config
        if df is None or df.empty:
            return PreparedSignal(error="ERR_EMPTY_DF")
        if "_time" not in df.columns:
            return PreparedSignal(error="ERR_MISSING_TIME")

        time = pd.to_datetime(df["_time"], errors="coerce")
        keep = time.notna().to_numpy()
        invalid_times = int((~keep).sum())
        frame, time = df.loc[keep], time[keep]
        reordered = not time.is_monotonic_increasing
        if reordered:
            order = np.argsort(time.to_numpy(), kind="stable")
            frame, time = frame.iloc[order], time.iloc[order]
        repeated = time.duplicated().to_numpy()
        frame, time = frame.loc[~repeated], time[~repeated]
        if len(time) < 3:
            return PreparedSignal(error="ERR_TOO_FEW_SAMPLES")

        t = (time - time.iloc[0]).dt.total_seconds().to_numpy()
        steps = np.diff(t)
        gaps = steps > cfg.max_gap_s
        segment = np.concatenate(([0], np.cumsum(gaps)))
        n_segments = int(segment[-1]) + 1
        gap_time = float(steps[gaps].sum())
        inner = steps[~gaps]
        if len(inner) == 0 or inner.sum() <= 0:
            return PreparedSignal(error="ERR_TOO_FEW_SAMPLES")
        dt = float(inner.mean())
        fs = 1.0 / dt
        duration = float(t[-1] - t[0] + dt)
        # Fracción de intervalos que se apartan más de un 50 % del medio (ráfagas).
        irregularity = float(np.mean(np.abs(inner - dt) > 0.5 * dt))

        limit = self.seconds_to_samples(cfg.max_interpolation_s, fs)
        data = pd.DataFrame(index=np.arange(len(t)))
        coverage: Dict[str, float] = {}
        interpolated = 0
        for channel in SIGNAL_CHANNELS:
            if channel not in frame.columns:
                continue
            values = pd.Series(pd.to_numeric(frame[channel], errors="coerce").to_numpy(dtype=float))
            missing = values.isna()
            coverage[channel] = round(float(1 - missing.mean()), 4)
            if missing.any() and not missing.all():
                # Solo se rellenan tramos cortos y dentro de un mismo segmento continuo.
                run_id = (missing != missing.shift()).cumsum()
                run_length = missing.groupby(run_id).transform("size")
                fill = np.array(missing & (run_length <= limit), dtype=bool)
                candidate = values.groupby(segment).transform(lambda s: s.interpolate(limit_area="inside"))
                fill = fill & candidate.notna().to_numpy()
                values = values.where(~fill, candidate)
                interpolated += int(fill.sum())
            data[channel] = values

        report = {
            "muestras": len(t), "fs_efectiva": round(fs, 3), "duracion_real_seg": round(duration, 3),
            "n_huecos": int(gaps.sum()), "tiempo_huecos_seg": round(gap_time, 3), "n_segmentos": n_segments,
            "exposicion_seg": round(duration - gap_time, 3), "cobertura": coverage,
            "muestras_interpoladas": interpolated, "tiempos_invalidos": invalid_times,
            "tiempos_repetidos": int(repeated.sum()), "reordenada": bool(reordered),
            "irregularidad_tiempo": round(irregularity, 3), "intervalo_mediano_s": round(float(np.median(inner)), 5),
            "fs_desviada": bool(abs(fs - cfg.fs) / cfg.fs > cfg.fs_tolerance),
        }
        logger.debug(
            _("DEBUG_SIGNAL_PREPARED: {0} muestras, fs efectiva {1:.2f} Hz (nominal {2:g}), {3} huecos ({4:.2f} s), {5} muestras interpoladas, irregularidad temporal {6:.0%}").format(len(t), fs, cfg.fs, report['n_huecos'], gap_time, interpolated, irregularity)
        )
        prepared = PreparedSignal(t=t, data=data, segment=segment, dt=dt, fs=fs, report=report)
        nyquist = fs / 2
        if max(cfg.accel_cutoff_hz, cfg.gyro_cutoff_hz, cfg.pressure_cutoff_hz) >= nyquist:
            prepared.error = "ERR_CUTOFF_ABOVE_NYQUIST"
        return prepared

    def _prepared(self, df: pd.DataFrame | PreparedSignal) -> PreparedSignal:
        """Acepta una serie ya preparada o la prepara."""
        return df if isinstance(df, PreparedSignal) else self.preparar(df)

    def _usable(self, prepared: PreparedSignal, channels: tuple[str, ...]) -> str:
        """Devuelve el motivo por el que unos canales no son utilizables, o cadena vacía."""
        missing = [channel for channel in channels if channel not in prepared.data.columns]
        if missing:
            return f"ERR_MISSING_{','.join(missing).upper()}"
        low = [c for c in channels if prepared.report["cobertura"][c] < self.config.min_coverage]
        if low:
            return f"ERR_LOW_COVERAGE_{','.join(low).upper()}"
        return ""

    def _turns(self, prepared: PreparedSignal, excluir_giros: bool) -> tuple[np.ndarray, Dict[str, Any], set[str]]:
        """Marca las muestras en giro y resume los episodios.

        Returns:
            tuple[np.ndarray, Dict[str, Any], set[str]]: Máscara de giro, resumen
            (número y tiempo real de giros, y episodios cortos ignorados) y
            métodos de filtrado aplicados.
        """
        cfg = self.config
        turning = np.zeros(len(prepared.t), dtype=bool)
        summary = {"tiempo_giros_seg": 0.0, "n_giros": 0, "n_giros_descartados": 0, "giros_evaluados": False}
        if not excluir_giros or self._usable(prepared, (cfg.turn_axis,)):
            return turning, summary, set()
        gyro, methods = self._filtered(prepared, cfg.turn_axis, cfg.gyro_cutoff_hz)
        above = np.nan_to_num(np.abs(gyro), nan=0.0) > cfg.turn_threshold
        candidates = self._valid_runs(prepared, (cfg.turn_axis,), above)
        episodes = [run for run in candidates if self._exposure(prepared, [run]) >= cfg.turn_min_duration_s]
        for start, stop in episodes:
            turning[start:stop] = True
        summary = {
            "tiempo_giros_seg": round(self._exposure(prepared, episodes), 2), "n_giros": len(episodes),
            "n_giros_descartados": len(candidates) - len(episodes), "giros_evaluados": True,
        }
        return turning, summary, methods

    # ------------------------------------------------------------------ métricas

    def calcular_cadencia(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True) -> Dict[str, Any]:
        """Calcula el número de pasos y la cadencia a partir de los picos de aceleración.

        La exposición se mide con ``_time``. Los giros se retiran sin compactar
        la señal: los picos se buscan en cada tramo continuo de marcha estable,
        con la separación mínima en segundos reales. Si tras retirar los giros
        queda menos de ``min_steady_s``, no se informa cadencia y se conserva el
        tiempo de giro. La modalidad se elige con una regla única, igual para
        cualquier duración: aceleración si es utilizable, y presión del talón si
        la aceleración no lo es o detecta menos de ``rescue_min_steps`` picos y
        la presión detecta más. Se informan la modalidad y los dos recuentos.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de una extremidad con
                ``_time`` y las señales, o la serie ya preparada.
            excluir_giros (bool, optional): Si es True, elimina del cálculo los
                intervalos asociados a giros. Por defecto es True.

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'num_pasos' (int): Picos detectados con la modalidad elegida.
                - 'cadencia' (float): Picos por minuto de marcha analizada.
                - 'tiempo_giros_seg' (float), 'n_giros' (int): Exposición a giros.
                - 'tiempo_analizado_seg' (float): Marcha estable analizada.
                - 'modalidad' (str), 'pasos_accel', 'pasos_presion': Modalidad
                  usada y recuento de cada una.
                - 'metodo_filtro' (str): Filtro realmente aplicado.
                - 'fs_efectiva', 'duracion_real_seg', 'n_huecos': Calidad temporal.
                - 'status' (str): OK, WARNING o ERROR; 'error_reason' si no es OK.
        """
        cfg = self.config
        keys = ("num_pasos", "cadencia")
        prepared = self._prepared(df)
        if prepared.error:
            status = "WARNING" if prepared.error == "ERR_TOO_FEW_SAMPLES" else "ERROR"
            return self._failure(keys, status, prepared.error, prepared)

        accel_problem = self._usable(prepared, ("Ay",))
        pressure_problem = self._usable(prepared, ("S0",))
        if cfg.step_modality == "accel" and accel_problem:
            return self._failure(keys, "ERROR", accel_problem, prepared)
        if cfg.step_modality == "pressure" and pressure_problem:
            return self._failure(keys, "ERROR", pressure_problem, prepared)
        if accel_problem and pressure_problem:
            return self._failure(keys, "ERROR", accel_problem, prepared)

        turning, turns, methods = self._turns(prepared, excluir_giros)
        steady = ~turning
        counts: Dict[str, float] = {"accel": float("nan"), "pressure": float("nan")}
        exposures: Dict[str, float] = {}

        if not accel_problem:
            signal, used = self._filtered(prepared, "Ay", cfg.accel_cutoff_hz)
            methods |= used
            runs = self._valid_runs(prepared, ("Ay",), steady)
            exposures["accel"] = self._exposure(prepared, runs)
            if runs:
                values = np.concatenate([signal[a:b] for a, b in runs])
                centred = signal - values.mean()
                # Energía real del paso del paciente, con un mínimo de seguridad.
                threshold = max(cfg.step_min_threshold, cfg.step_std_factor * float(np.std(values, ddof=1)) if len(values) > 1 else 0.0)
                distance = self.seconds_to_samples(cfg.step_min_distance_s, prepared.fs)
                counts["accel"] = sum(
                    len(find_peaks(centred[a:b], height=threshold, distance=distance, prominence=cfg.step_prominence)[0])
                    for a, b in runs
                )
        if not pressure_problem:
            signal, used = self._filtered(prepared, "S0", cfg.pressure_cutoff_hz)
            methods |= used
            runs = self._valid_runs(prepared, ("S0",), steady)
            exposures["pressure"] = self._exposure(prepared, runs)
            distance = self.seconds_to_samples(cfg.pressure_step_distance_s, prepared.fs)
            counts["pressure"] = sum(
                len(find_peaks(signal[a:b], height=cfg.pressure_step_height, distance=distance)[0]) for a, b in runs
            )

        modality = cfg.step_modality
        if modality == "auto":
            modality = "pressure" if accel_problem else "accel"
            if not accel_problem and not pressure_problem and counts["accel"] < cfg.rescue_min_steps < counts["pressure"] + 1 \
                    and counts["pressure"] > counts["accel"]:
                modality = "pressure"
                logger.debug(_("INFO_BIOMECH_RESCUE: Fusión de sensores activada. Pasos rescatados con S0 ({p})").format(p=int(counts["pressure"])))
        analysed = exposures[modality]
        common = {
            **turns, "tiempo_analizado_seg": round(analysed, 2), "modalidad": modality,
            "pasos_accel": counts["accel"], "pasos_presion": counts["pressure"],
            "metodo_filtro": "+".join(sorted(methods)), "exposicion_seg": prepared.report["exposicion_seg"],
        }
        if turns["tiempo_giros_seg"] > 0 and analysed < cfg.min_steady_s:
            logger.debug(
                _("DEBUG_NO_STEADY_GAIT: {0:.2f} s de marcha estable tras {1} s de giros (mínimo {2} s); cadencia no calculada").format(analysed, turns['tiempo_giros_seg'], cfg.min_steady_s)
            )
            return self._failure(keys, "WARNING", "ERR_NO_STEADY_GAIT", prepared, **common)
        if analysed <= 0:
            return self._failure(keys, "WARNING", "ERR_NO_VALID_SEGMENTS", prepared, **common)

        num_pasos = int(counts[modality])
        cadencia = num_pasos / (analysed / 60)

        # --- Validación Biomecánica (Bloque 3) y Logging ---
        status = "OK" if cfg.cadence_min <= cadencia <= cfg.cadence_max else "WARNING"
        if cadencia < cfg.cadence_min and prepared.report["duracion_real_seg"] > 30.0:
            logger.warning(_("WARN_BIOMECH_OUT_OF_BOUNDS: Cadencia inverosímil detectada ({cad} ppm) en prueba de duración válida (>30s).").format(cad=round(cadencia, 2)))

        return {
            "num_pasos": num_pasos, "cadencia": round(cadencia, 2), **common, **self._quality(prepared),
            "status": status, "error_reason": "" if status == "OK" else "WARN_CADENCE_OUT_OF_BOUNDS",
        }

    def _pressure_state(self, prepared: PreparedSignal) -> tuple[np.ndarray, str, str]:
        """Clasifica cada muestra como apoyo o balanceo a partir de la presión total.

        Returns:
            tuple[np.ndarray, str, str]: Vector con 1 (apoyo), 0 (balanceo) o
            ``NaN`` (sin presión válida), estado y motivo si no puede calcularse.
        """
        cfg = self.config
        problem = self._usable(prepared, PRESSURE_CHANNELS)
        if problem:
            return np.array([]), "ERROR", problem
        pressure = prepared.data[list(PRESSURE_CHANNELS)].sum(axis=1, skipna=False).to_numpy()
        window = self.seconds_to_samples(cfg.pressure_window_s, prepared.fs)
        smooth = np.full(len(pressure), np.nan)
        for start, stop in self._valid_runs(prepared, PRESSURE_CHANNELS):
            smooth[start:stop] = pd.Series(pressure[start:stop]).rolling(window=window, center=True, min_periods=1).mean().to_numpy()
        valid = smooth[~np.isnan(smooth)]
        if len(valid) == 0:
            return np.array([]), "ERROR", "ERR_EMPTY_PRESSURE"
        high, low = np.percentile(valid, 95), np.percentile(valid, 5)
        if np.isclose(high, low):
            return np.array([]), "WARNING", "ERR_FLAT_PRESSURE"
        threshold = (high + low) / 2
        stance = smooth < threshold if cfg.pressure_stance_is_low else smooth > threshold
        return np.where(np.isnan(smooth), np.nan, stance.astype(float)), "OK", ""

    def calcular_fases(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True) -> Dict[str, Any]:
        """Determina la distribución porcentual de las fases de apoyo (Stance)
        y balanceo (Swing) del ciclo de marcha.

        Detecta en la presión los contactos y despegues, forma ciclos completos
        (contacto → despegue → contacto siguiente) dentro de tramos continuos de
        marcha estable y calcula el apoyo de cada ciclo con tiempos reales. El
        reposo, los giros y los ciclos incompletos o de duración no plausible
        quedan fuera. La polaridad de la presión es ``pressure_stance_is_low``.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie del lado correspondiente
                con ``_time``, 'S0', 'S1' y 'S2', o la serie ya preparada.
            excluir_giros (bool, optional): Excluir los ciclos en giro.

        Returns:
            Dict[str, Any]: Distribución de fases:
                - 'apoyo_prc' (float): Media del porcentaje de apoyo por ciclo.
                - 'balanceo_prc' (float): Porcentaje de balanceo.
                - 'n_ciclos' (int), 'n_ciclos_excluidos' (int): Ciclos usados y descartados.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("apoyo_prc", "balanceo_prc")
        prepared = self._prepared(df)
        if prepared.error:
            return self._failure(keys, "ERROR", prepared.error, prepared)
        state, status, reason = self._pressure_state(prepared)
        if reason:
            return self._failure(keys, status, reason, prepared)

        turning, _turns, _methods = self._turns(prepared, excluir_giros)
        stance_pct: list[float] = []
        excluded = 0
        for start, stop in self._valid_runs(prepared, PRESSURE_CHANNELS, ~turning):
            change = np.diff(state[start:stop])
            contacts = np.flatnonzero(change == 1) + start + 1
            lifts = np.flatnonzero(change == -1) + start + 1
            for first, second in zip(contacts[:-1], contacts[1:]):
                between = lifts[(lifts > first) & (lifts < second)]
                duration = prepared.t[second] - prepared.t[first]
                if len(between) != 1 or not cfg.cycle_min_s <= duration <= cfg.cycle_max_s:
                    excluded += 1
                    continue
                stance_pct.append((prepared.t[between[0]] - prepared.t[first]) / duration * 100)
        if not stance_pct:
            return self._failure(keys, "WARNING", "ERR_NO_VALID_CYCLES", prepared, n_ciclos=0, n_ciclos_excluidos=excluded)

        apoyo = float(np.mean(stance_pct))
        return {
            "apoyo_prc": round(apoyo, 2), "balanceo_prc": round(100.0 - apoyo, 2),
            "n_ciclos": len(stance_pct), "n_ciclos_excluidos": excluded, "metodo_filtro": "media_movil",
            **self._quality(prepared), "status": "OK", "error_reason": "",
        }

    def calcular_decaimiento_6mwt(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Calcula el índice de fatiga o decaimiento de la cadencia a lo largo del test 6MWT.

        Compara la cadencia del primer bloque de ``decay_block_s`` segundos con
        la del último, ambos delimitados por ``_time``. Exige una duración real
        mínima y cobertura suficiente en cada bloque; si un bloque no es válido,
        el decaimiento no se informa.

        Args:
            df (pd.DataFrame): Datos del test de marcha de 6 minutos con ``_time``.

        Returns:
            Dict[str, Any]: Fatiga cuantificada:
                - '6mwt_decaimiento' (float): Porcentaje de pérdida de cadencia.
                - 'cadencia_inicial', 'cadencia_final' (float): Cadencia de cada bloque.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("6mwt_decaimiento",)
        prepared = self.preparar(df)
        if prepared.error:
            return self._failure(keys, "WARNING", prepared.error, prepared)
        if prepared.report["duracion_real_seg"] < cfg.decay_min_duration_s:
            return self._failure(keys, "WARNING", "ERR_TOO_SHORT_FOR_DECAY", prepared)

        seconds = (pd.to_datetime(df["_time"], errors="coerce") - pd.to_datetime(df["_time"], errors="coerce").min()).dt.total_seconds()
        end = float(seconds.max())
        blocks = {}
        for name, mask in (("inicial", seconds < cfg.decay_block_s), ("final", seconds >= end - cfg.decay_block_s)):
            result = self.calcular_cadencia(df[mask.to_numpy()], excluir_giros=True)
            covered = result.get("exposicion_seg", 0.0) or 0.0
            if result["status"] == "ERROR" or not np.isfinite(result["cadencia"]) or covered < cfg.min_coverage * cfg.decay_block_s:
                detail = result.get("error_reason") or "ERR_LOW_COVERAGE"
                return self._failure(keys, "WARNING", f"ERR_INVALID_BLOCK_{name.upper()}:{detail}", prepared)
            blocks[name] = float(result["cadencia"])
        if blocks["inicial"] <= 0:
            return self._failure(keys, "WARNING", "ERR_ZERO_INITIAL_CADENCE", prepared)

        decay = (blocks["inicial"] - blocks["final"]) / blocks["inicial"] * 100
        return {
            "6mwt_decaimiento": round(decay, 2), "cadencia_inicial": blocks["inicial"], "cadencia_final": blocks["final"],
            **self._quality(prepared), "status": "OK", "error_reason": "",
        }

    def calcular_longitud_zancada(self, df: pd.DataFrame | PreparedSignal) -> Dict[str, Any]:
        """Estima la longitud de zancada media en centímetros (estimador NO calibrado).

        Implementa un proxy empírico de tipo Weinberg: ``K · (rango pico a pico
        de Ay en m/s²) ^ exponente``, con el rango medido en ventanas de
        ``stride_window_s`` dentro de cada tramo continuo. ``K`` es un valor
        fijo sin calibrar con una distancia de referencia, por lo que los
        centímetros son orientativos. Se devuelven el valor sin recortar y el
        recortado.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de la extremidad con
                ``_time`` y 'Ay', o la serie ya preparada.

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'stride_length_cm' (float): Longitud recortada a los límites configurados.
                - 'stride_length_raw_cm' (float): Longitud sin recortar.
                - 'calibrado' (bool): Siempre False mientras K no se calibre.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("stride_length_cm", "stride_length_raw_cm")
        prepared = self._prepared(df)
        if prepared.error:
            status = "WARNING" if prepared.error == "ERR_TOO_FEW_SAMPLES" else "ERROR"
            return self._failure(keys, status, prepared.error, prepared, calibrado=False)
        problem = self._usable(prepared, ("Ay",))
        if problem:
            return self._failure(keys, "ERROR", problem, prepared, calibrado=False)

        signal, methods = self._filtered(prepared, "Ay", cfg.accel_cutoff_hz)
        window = self.seconds_to_samples(cfg.stride_window_s, prepared.fs, minimum=2)
        minimum = self.seconds_to_samples(cfg.stride_window_s / 2, prepared.fs, minimum=2)
        ranges = [
            pd.Series(signal[a:b]).rolling(window=window, min_periods=minimum).apply(np.ptp, raw=True).dropna()
            for a, b in self._valid_runs(prepared, ("Ay",))
        ]
        ranges = pd.concat(ranges) if ranges else pd.Series(dtype=float)
        if ranges.empty:
            return self._failure(keys, "WARNING", "ERR_TOO_FEW_SAMPLES", prepared, calibrado=False)
        if ranges.mean() <= 0:
            return self._failure(keys, "WARNING", "ERR_NO_DYNAMIC_RANGE", prepared, calibrado=False)

        raw = float(cfg.stride_k * (ranges.mean() * cfg.gravity) ** cfg.stride_exponent)
        clipped = float(np.clip(raw, cfg.stride_clip_min_cm, cfg.stride_clip_max_cm))
        status = "OK" if cfg.stride_ok_min_cm <= clipped <= cfg.stride_ok_max_cm else "WARNING"
        return {
            "stride_length_cm": round(clipped, 2), "stride_length_raw_cm": round(raw, 2), "calibrado": False,
            "metodo_filtro": "+".join(sorted(methods)), **self._quality(prepared),
            "status": status, "error_reason": "" if status == "OK" else "WARN_STRIDE_OUT_OF_BOUNDS",
        }

    def calcular_altura_zancada(self, df: pd.DataFrame | PreparedSignal) -> Dict[str, Any]:
        """Calcula la altura de la zancada (Foot Clearance) durante el balanceo.

        Aísla con la presión los balanceos completos (precedidos y seguidos de
        apoyo dentro de un tramo continuo y con 'Ay' válida) y aplica una doble
        integración de 'Ay' sobre el tiempo real de cada uno. Los balanceos
        cortados por el inicio o el fin del registro, o de duración no
        plausible, se descartan y se devuelven con su causa.

        Hipótesis no validadas: 'Ay' es el eje vertical y está en g; centrar el
        tramo no elimina la gravedad si cambia la orientación del pie.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de una extremidad con
                ``_time``, 'Ay', 'S0', 'S1' y 'S2', o la serie ya preparada.

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'step_height_cm' (float): Altura media de los balanceos válidos.
                - 'n_balanceos' (int), 'n_balanceos_excluidos' (int).
                - 'eventos' (list[dict]): Un registro por balanceo con inicio,
                  duración, altura y causa de exclusión.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("step_height_cm",)
        prepared = self._prepared(df)
        if prepared.error:
            return self._failure(keys, "ERROR", prepared.error, prepared, eventos=[])
        problem = self._usable(prepared, ("Ay",))
        if problem:
            return self._failure(keys, "ERROR", problem, prepared, eventos=[])
        state, status, reason = self._pressure_state(prepared)
        if reason:
            return self._failure(keys, status, reason, prepared, eventos=[])

        accel = prepared.data["Ay"].to_numpy(dtype=float)
        events: list[Dict[str, Any]] = []
        for start, stop in self._valid_runs(prepared, ("Ay", *PRESSURE_CHANNELS)):
            for first, last in _true_runs(state[start:stop] == 0):
                first, last = first + start, last + start
                event = {
                    "inicio_s": round(float(prepared.t[first]), 3),
                    "duracion_s": round(float(prepared.t[last - 1] - prepared.t[first] + prepared.dt), 3),
                    "altura_cm": float("nan"), "motivo_exclusion": "",
                }
                if first == start or last == stop:
                    event["motivo_exclusion"] = "balanceo_incompleto"
                elif not cfg.swing_min_s <= event["duracion_s"] <= cfg.swing_max_s:
                    event["motivo_exclusion"] = "duracion_fuera_de_rango"
                elif last - first < 3:
                    event["motivo_exclusion"] = "muestras_insuficientes"
                else:
                    time = prepared.t[first:last]
                    # Convertir a m/s² y centrar para anular la gravedad estática.
                    metres = (accel[first:last] - accel[first:last].mean()) * cfg.gravity
                    velocity = detrend(cumulative_trapezoid(metres, x=time, initial=0))
                    position = detrend(cumulative_trapezoid(velocity, x=time, initial=0))
                    event["altura_cm"] = round(float(np.ptp(position) * 100), 3)
                events.append(event)

        heights = [event["altura_cm"] for event in events if not event["motivo_exclusion"]]
        excluded = len(events) - len(heights)
        if not heights:
            return self._failure(
                keys, "WARNING", "ERR_NO_VALID_SWING_PHASES", prepared,
                n_balanceos=0, n_balanceos_excluidos=excluded, eventos=events,
            )
        mean_height = round(float(np.mean(heights)), 2)
        # Validación fisiológica con los límites configurados.
        status = "OK" if cfg.height_ok_min_cm <= mean_height <= cfg.height_ok_max_cm else "WARNING"
        return {
            "step_height_cm": mean_height, "n_balanceos": len(heights), "n_balanceos_excluidos": excluded,
            "eventos": events, "metodo_filtro": "sin_filtro", **self._quality(prepared),
            "status": status, "error_reason": "" if status == "OK" else "WARN_HEIGHT_OUT_OF_BOUNDS",
        }
