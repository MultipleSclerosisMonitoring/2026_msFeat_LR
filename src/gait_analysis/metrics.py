import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, Mapping

import numpy as np
import pandas as pd
from scipy.integrate import cumulative_trapezoid
from scipy.signal import butter, filtfilt, find_peaks

from msConfig import ConfigError
from msTools.i18n import _

# Inicializamos el logger estructurado para este módulo
logger = logging.getLogger("msGait.GaitMetricsCalculator")

IMU_CHANNELS = ("Ax", "Ay", "Az", "Gx", "Gy", "Gz")
PRESSURE_CHANNELS = ("S0", "S1", "S2")  # S0 y S1: antepié; S2: talón
SIGNAL_CHANNELS = (*IMU_CHANNELS, *PRESSURE_CHANNELS)
# Los huecos y la cobertura se miden por grupo de sensores.
SENSOR_GROUPS = {"imu": IMU_CHANNELS, "presion": PRESSURE_CHANNELS}
# Canales derivados: aceleración y velocidad de giro proyectadas sobre la vertical estimada.
VERTICAL_ACCEL = "Av"
VERTICAL_RATE = "Wv"
TIME_GRIDS = ("uniform", "received")
# Métricas por pie que forman las tablas y estadísticas del informe (lista explícita).
REPORT_METRICS = ("zancadas", "cadencia_zancada", "apoyo_prc", "balanceo_prc", "decaimiento_prc", "stride_length_cm", "excursion_tobillo_cm")
SYMMETRY_METRIC = "symmetry_index"
MTC_NOT_COMPUTABLE = "MTC no calculable: sensor en el tobillo, falta el traslado a la punta"


@dataclass(frozen=True)
class ProcessingConfig:
    """Parámetros del procesador de señales (sección ``processing`` del YAML).

    Todas las ventanas y distancias se expresan en segundos y se convierten a
    muestras con la frecuencia efectiva medida en cada serie.

    Attributes:
        fs (float): Frecuencia de muestreo nominal en Hz (unos 50 Hz, aunque
            depende del sensor). Solo se usa para señalar las series cuya
            frecuencia efectiva medida es distinta.
        time_grid (str): ``uniform`` recoloca las muestras de cada segmento
            continuo en una rejilla de paso constante, sin tocar sus valores:
            las marcas de ``_time`` son de recepción en el móvil y llegan en
            ráfagas, mientras que el sensor muestrea a ritmo constante.
            ``received`` usa las marcas tal como llegaron.
        fs_tolerance (float): Desviación relativa admitida entre la frecuencia
            efectiva y la nominal antes de marcar la serie.
        max_gap_s (float): Hay un hueco cuando pasan más de estos segundos sin
            un valor nuevo, medido por grupo de sensores (IMU y presión). Un
            hueco corta la serie para ese grupo. En la IMU solo se aplica con
            el pie en movimiento: quieto, el valor repetido es una medida válida.
        reception_window_s (float): Tras un intervalo largo entre filas, tiempo
            adicional en el que se cuentan las muestras que llegan con retraso.
        reception_min_fraction (float): Si en el intervalo más esa ventana
            llega al menos esta fracción de las muestras esperadas, es
            recepción retrasada y no un hueco.
        imu_moving_gyro (float): Módulo del giroscopio (°/s) por encima del
            cual se considera que el pie se mueve.
        imu_max_stale_moving (float): Fracción máxima de filas con la IMU
            repetida mientras el pie se mueve; por encima, la IMU de la serie
            no es válida.
        max_interpolation_s (float): Tiempo máximo sin valor nuevo que se
            rellena por interpolación lineal; no puede superar ``max_gap_s``.
        min_coverage (float): Fracción mínima del tiempo fuera de huecos que
            debe tener la presión para usarla.
        min_strides (Dict[str, int]): Mínimo de zancadas válidas por prueba para
            informar sus métricas de marcha (6MWT: 30 en el ensayo; 2MWT: 10;
            T25FW: 4, juntando los intentos del paciente en el mismo día). Una
            prueba sin mínimo definido no informa métricas de marcha.
        min_strides_minute (int): Mínimo de zancadas válidas en cada minuto
            que se compara en el decaimiento.
        t25fw_skip_first_strides (int): Zancadas completas de cada pie que se
            descartan al inicio de cada intento de T25FW (arranque).
        filter_order (int): Orden del filtro Butterworth.
        pressure_window_s (float): Ventana de suavizado de cada sensor de presión.
        pressure_min_range (float): Rango mínimo (percentil 95 menos percentil
            5 de la lectura suavizada) de un sensor de presión. En el antepié,
            por debajo se trata como plano. En el talón solo decide cuando la
            ventana es demasiado corta para medir el ritmo.
        heel_min_periodicity (float): Autocorrelación mínima de la presión del
            talón en el periodo de zancada (``cycle_min_s`` a ``cycle_max_s``)
            para considerar que tiene modulación de marcha. Es el criterio de
            calidad principal y no depende de la escala de la lectura.
        heel_valley_prominence (float): Prominencia mínima, sobre la señal del
            talón normalizada, del valle de mitad de balanceo que debe preceder
            a cada contacto de talón.
        rest_gyro_quantile (float): Fracción de muestras con menor giro que se
            consideran candidatas a reposo para estimar la gravedad.
        rest_accel_tolerance_g (float): En reposo el módulo de la aceleración
            debe estar a esta distancia de 1 g como máximo.
        turn_axis (str): ``vertical`` usa la velocidad de giro proyectada sobre
            la vertical estimada (``Gy`` si no puede estimarse); también admite
            un canal concreto del giroscopio.
        turn_smooth_cutoff_hz (float): Corte del pasabajos que elimina la
            oscilación de cada zancada antes de buscar giros.
        turn_threshold (float): Velocidad de giro suavizada (°/s) que debe
            superarse en algún momento para que haya un episodio candidato.
        turn_extend_threshold (float): Velocidad (°/s) hasta la que se extiende
            el episodio a ambos lados de su núcleo, para no perder el ángulo
            del arranque y del final del giro.
        turn_min_angle_deg (float): Ángulo acumulado mínimo de un episodio para
            contarlo como giro. Es el criterio que separa los giros reales.
        turn_min_duration_s (float): Duración mínima adicional de un giro; 0 no
            la exige.
        min_steady_s (float): Marcha estable mínima tras retirar los giros.
        heel_channel (str): Sensor de presión del talón, con el que se detecta
            el contacto de talón (detector principal de zancadas).
        pressure_stance_is_low (bool): Polaridad: ``True`` si la presión baja
            cuando el pie carga peso.
        cycle_min_s (float): Tiempo de zancada mínimo; es también la separación
            mínima entre dos contactos de talón.
        cycle_max_s (float): Tiempo de zancada máximo.
        swing_min_s (float): Duración mínima del balanceo de un ciclo válido.
        swing_max_s (float): Duración máxima del balanceo de un ciclo válido.
        swing_min_prc (float): Porcentaje mínimo del ciclo que ocupa el balanceo.
        swing_max_prc (float): Porcentaje máximo del ciclo que ocupa el balanceo.
        cadence_min (float): Límite inferior de cadencia de zancada plausible.
        cadence_max (float): Límite superior de cadencia de zancada plausible.
        decay_block_s (float): Duración de cada bloque (minuto) del 6MWT.
        sixmwt_duration_s (float): Duración que debe tener la ventana del
            registro de un 6MWT completo.
        sixmwt_tolerance_s (float): Tolerancia de esa duración.
        sixmwt_min_valid_minutes (int): Minutos con datos válidos necesarios
            para aceptar el 6MWT como completo y calcular el decaimiento.
        stride_ok_min_cm (float): Límite inferior de longitud plausible.
        stride_ok_max_cm (float): Límite superior de longitud plausible.
        mtc_ok_min_cm (float): Límite inferior de la distancia mínima punta-suelo
            (MTC) media de una serie. Reservado: la MTC no se calcula todavía.
        mtc_ok_max_cm (float): Límite superior de la MTC media de una serie.
        mtc_stride_max_cm (float): Límite superior de la MTC de una zancada.
        gravity (float): Aceleración de la gravedad para pasar de g a m/s².
    """
    fs: float = 50.0
    time_grid: str = "uniform"
    fs_tolerance: float = 0.10
    max_gap_s: float = 0.1
    max_interpolation_s: float = 0.1
    reception_window_s: float = 0.2
    reception_min_fraction: float = 0.9
    imu_moving_gyro: float = 100.0
    imu_max_stale_moving: float = 0.10
    min_coverage: float = 0.8
    min_strides: Dict[str, int] = field(default_factory=lambda: {"6MWT": 30, "2MWT": 10, "T25FW": 4})
    min_strides_minute: int = 20
    t25fw_skip_first_strides: int = 1
    filter_order: int = 4
    pressure_window_s: float = 0.15
    pressure_min_range: float = 40.0
    heel_min_periodicity: float = 0.3
    heel_valley_prominence: float = 0.4
    rest_gyro_quantile: float = 0.25
    rest_accel_tolerance_g: float = 0.1
    turn_axis: str = "vertical"
    turn_smooth_cutoff_hz: float = 0.5
    turn_threshold: float = 25.0
    turn_extend_threshold: float = 10.0
    turn_min_angle_deg: float = 90.0
    turn_min_duration_s: float = 0.0
    min_steady_s: float = 3.0
    heel_channel: str = "S2"
    pressure_stance_is_low: bool = True
    cycle_min_s: float = 0.8
    cycle_max_s: float = 2.5
    swing_min_s: float = 0.25
    swing_max_s: float = 0.8
    swing_min_prc: float = 20.0
    swing_max_prc: float = 55.0
    cadence_min: float = 24.0
    cadence_max: float = 75.0
    decay_block_s: float = 60.0
    sixmwt_duration_s: float = 360.0
    sixmwt_tolerance_s: float = 5.0
    sixmwt_min_valid_minutes: int = 5
    stride_ok_min_cm: float = 30.0
    stride_ok_max_cm: float = 150.0
    mtc_ok_min_cm: float = 0.4
    mtc_ok_max_cm: float = 5.5
    mtc_stride_max_cm: float = 6.0
    gravity: float = 9.81

    def __post_init__(self) -> None:
        """Valida tipos, signos y límites cruzados.

        Raises:
            ConfigError: Si algún parámetro no es válido o dos límites se contradicen.
        """
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in ("turn_axis", "heel_channel", "pressure_stance_is_low", "time_grid", "min_strides"):
                continue
            if item.name == "turn_min_duration_s" and value == 0 and not isinstance(value, bool):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
                raise ConfigError(_("processing.{0} debe ser un número finito mayor que cero.").format(item.name))
        if not isinstance(self.min_strides, dict) or any(
            not isinstance(test, str) or isinstance(value, bool) or not isinstance(value, int) or value < 1
            for test, value in self.min_strides.items()
        ):
            raise ConfigError(_("processing.min_strides debe asignar a cada prueba un entero mayor que cero."))
        for name in ("filter_order", "sixmwt_min_valid_minutes", "min_strides_minute", "t25fw_skip_first_strides"):
            if not isinstance(getattr(self, name), int):
                raise ConfigError(_("processing.{0} debe ser un entero.").format(name))
        if not isinstance(self.pressure_stance_is_low, bool):
            raise ConfigError(_("processing.pressure_stance_is_low debe ser true o false."))
        if self.turn_axis != "vertical" and self.turn_axis not in IMU_CHANNELS:
            raise ConfigError(_("processing.turn_axis debe ser uno de {0}.").format(', '.join(("vertical", *IMU_CHANNELS))))
        if self.heel_channel not in PRESSURE_CHANNELS:
            raise ConfigError(_("processing.heel_channel debe ser uno de {0}.").format(', '.join(PRESSURE_CHANNELS)))
        if self.time_grid not in TIME_GRIDS:
            raise ConfigError(_("processing.time_grid debe ser uno de {0}.").format(', '.join(TIME_GRIDS)))
        if self.max_gap_s <= 1 / self.fs:
            raise ConfigError(_("processing.max_gap_s debe ser mayor que el intervalo nominal entre muestras."))
        if self.max_interpolation_s > self.max_gap_s:
            raise ConfigError(_("processing.max_interpolation_s no puede ser mayor que processing.max_gap_s."))
        for name in ("min_coverage", "rest_gyro_quantile", "reception_min_fraction", "imu_max_stale_moving",
                     "heel_min_periodicity", "heel_valley_prominence"):
            if getattr(self, name) > 1:
                raise ConfigError(_("processing.{0} debe estar entre 0 y 1.").format(name))
        nyquist = self.fs / 2
        for name in ("turn_smooth_cutoff_hz",):
            if getattr(self, name) >= nyquist:
                raise ConfigError(_("processing.{0} debe ser menor que la frecuencia de Nyquist ({1} Hz).").format(name, nyquist))
        for low, high in (
            ("cadence_min", "cadence_max"), ("cycle_min_s", "cycle_max_s"), ("swing_min_s", "swing_max_s"),
            ("swing_min_prc", "swing_max_prc"),
            ("stride_ok_min_cm", "stride_ok_max_cm"), ("mtc_ok_min_cm", "mtc_ok_max_cm"),
        ):
            if getattr(self, low) >= getattr(self, high):
                raise ConfigError(_("processing.{0} debe ser menor que processing.{1}.").format(low, high))
        if self.swing_max_prc >= 100:
            raise ConfigError(_("processing.swing_max_prc debe ser menor que 100."))
        if self.turn_extend_threshold > self.turn_threshold:
            raise ConfigError(_("processing.turn_extend_threshold no puede ser mayor que processing.turn_threshold."))
        if self.sixmwt_duration_s < 2 * self.decay_block_s:
            raise ConfigError(_("processing.sixmwt_duration_s debe ser al menos el doble de processing.decay_block_s."))
        if self.sixmwt_min_valid_minutes > round(self.sixmwt_duration_s / self.decay_block_s):
            raise ConfigError(_("processing.sixmwt_min_valid_minutes no puede superar el número de bloques del ensayo."))

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
        known = {item.name: item.type for item in fields(cls)}
        unknown = sorted(set(section) - set(known))
        if unknown:
            raise ConfigError(_("Claves desconocidas en processing: {0}.").format(', '.join(unknown)))
        for name, kind in known.items():
            if kind in (float, "float") and isinstance(section.get(name), int) and not isinstance(section[name], bool):
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
        data (pd.DataFrame): Canales numéricos tras tratar los huecos por grupo
            de sensores, más ``Av`` (aceleración vertical) y ``Wv`` (velocidad
            de giro en torno a la vertical).
        segment (np.ndarray): Identificador de segmento continuo de cada muestra.
        dt (float): Intervalo medio entre muestras dentro de los segmentos, en segundos.
        fs (float): Frecuencia efectiva: muestras por segundo de señal continua.
        report (Dict[str, Any]): Calidad temporal, cobertura por grupo y orientación.
        error (str): Motivo por el que la serie no puede usarse; vacío si es válida.
    """
    t: np.ndarray = field(default_factory=lambda: np.array([]))
    data: pd.DataFrame = field(default_factory=pd.DataFrame)
    segment: np.ndarray = field(default_factory=lambda: np.array([]))
    dt: float = float("nan")
    fs: float = float("nan")
    report: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cache: Dict[Any, Any] = field(default_factory=dict)


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

    def _fill_group(self, values: np.ndarray, t: np.ndarray, segment: np.ndarray,
                    still: np.ndarray | None = None) -> tuple[np.ndarray, int, int, np.ndarray]:
        """Trata los huecos de un grupo de sensores.

        Una fila aporta un valor nuevo si algún canal del grupo cambia respecto
        a la fila anterior. Las filas sin valor nuevo situadas entre dos valores
        nuevos separados como mucho ``max_interpolation_s`` se interpolan; si
        pasan más de ``max_gap_s`` sin valor nuevo hay un hueco y esas filas
        quedan sin valor, lo que corta la serie para el grupo.

        Args:
            values (np.ndarray): Matriz filas × canales del grupo (``NaN`` si falta).
            t (np.ndarray): Instante de cada fila en segundos.
            segment (np.ndarray): Segmento continuo de cada fila.
            still (np.ndarray | None): Filas en las que el sensor está quieto;
                en ellas un valor repetido es una medida válida y se conserva.

        Returns:
            tuple[np.ndarray, int, int, np.ndarray]: Valores tratados, número de
            filas interpoladas, número de huecos del grupo y máscara de filas
            sin valor nuevo.
        """
        cfg = self.config
        count = len(t)
        complete = ~np.isnan(values).any(axis=1)
        changed = np.ones(count, dtype=bool)
        changed[1:] = (np.nan_to_num(values[1:], nan=np.inf) != np.nan_to_num(values[:-1], nan=np.inf)).any(axis=1)
        changed[1:] |= segment[1:] != segment[:-1]
        new = complete & changed
        if not new.any():
            return np.full_like(values, np.nan), 0, 0, ~new
        index = np.arange(count)
        previous = np.maximum.accumulate(np.where(new, index, -1))
        following = np.minimum.accumulate(np.where(new, index, count)[::-1])[::-1]
        has_previous = previous >= 0
        has_following = following < count
        prev_safe, next_safe = np.clip(previous, 0, count - 1), np.clip(following, 0, count - 1)
        same_segment = has_previous & has_following & (segment[prev_safe] == segment) & (segment[next_safe] == segment)
        span = np.where(same_segment, t[next_safe] - t[prev_safe], np.inf)
        held = ~new
        interpolate = held & same_segment & (span <= cfg.max_interpolation_s)
        # Sin valor nuevo posterior en el segmento: se mantiene el último solo dentro del umbral.
        hold = held & ~same_segment & has_previous & (segment[prev_safe] == segment) & (t - t[prev_safe] <= cfg.max_gap_s)
        hold |= held & same_segment & ~interpolate & (span <= cfg.max_gap_s)
        gap = held & ~interpolate & ~hold
        if still is not None:
            # Con el sensor quieto, repetir el valor es medir lo mismo: ni se interpola ni es hueco.
            exempt = held & still & complete
            interpolate &= ~exempt
            hold &= ~exempt
            gap &= ~exempt
        out = values.copy()
        if interpolate.any():
            weight = ((t - t[prev_safe]) / np.where(np.isfinite(span) & (span > 0), span, 1.0))[interpolate, None]
            out[interpolate] = values[prev_safe[interpolate]] * (1 - weight) + values[next_safe[interpolate]] * weight
        out[hold] = values[prev_safe[hold]]
        out[gap] = np.nan
        return out, int(interpolate.sum()), len(_true_runs(gap)), held

    def _gravity(self, numeric: Dict[str, np.ndarray]) -> tuple[np.ndarray | None, float]:
        """Estima la dirección de la gravedad con las muestras en reposo de la serie.

        Reposo: muestras con poco giro (por debajo del cuantil
        ``rest_gyro_quantile`` de su módulo) y módulo de la aceleración a menos
        de ``rest_accel_tolerance_g`` de 1 g.

        Args:
            numeric (Dict[str, np.ndarray]): Canales IMU tal como llegaron.

        Returns:
            tuple[np.ndarray | None, float]: Vector unitario de la gravedad en
            los ejes del sensor (``None`` si no hay reposo utilizable) e
            inclinación en grados respecto al eje ``Ay``.
        """
        cfg = self.config
        if any(channel not in numeric for channel in IMU_CHANNELS):
            return None, float("nan")
        acc = np.column_stack([numeric[c] for c in ("Ax", "Ay", "Az")])
        gyr = np.column_stack([numeric[c] for c in ("Gx", "Gy", "Gz")])
        valid = ~np.isnan(acc).any(axis=1) & ~np.isnan(gyr).any(axis=1)
        if valid.sum() < 10:
            return None, float("nan")
        gyro_norm = np.linalg.norm(gyr, axis=1)
        accel_norm = np.linalg.norm(acc, axis=1)
        rest = valid & (gyro_norm <= np.quantile(gyro_norm[valid], cfg.rest_gyro_quantile)) & \
            (np.abs(accel_norm - 1.0) <= cfg.rest_accel_tolerance_g)
        if rest.sum() < 10:
            return None, float("nan")
        direction = np.median(acc[rest], axis=0)
        norm = float(np.linalg.norm(direction))
        if norm < 0.5:
            return None, float("nan")
        direction = direction / norm
        return direction, float(np.degrees(np.arccos(min(1.0, abs(direction[1])))))

    def _real_gaps(self, t: np.ndarray, steps: np.ndarray) -> tuple[np.ndarray, int]:
        """Distingue los huecos reales de la recepción retrasada.

        En cada intervalo entre filas mayor que ``max_gap_s`` se cuentan las
        muestras recibidas en el intervalo más los ``reception_window_s``
        siguientes y se comparan con las que corresponden por duración
        (frecuencia medida × tiempo). Si llega al menos
        ``reception_min_fraction``, el móvil recibió tarde un bloque completo:
        no hay hueco y las marcas se reparten después de forma uniforme. Si
        llega menos, es un hueco real.

        Args:
            t (np.ndarray): Marcas de recepción en segundos.
            steps (np.ndarray): Intervalos entre filas consecutivas.

        Returns:
            tuple[np.ndarray, int]: Máscara de huecos reales sobre ``steps`` y
            número de intervalos largos recuperados.
        """
        cfg = self.config
        long = steps > cfg.max_gap_s
        if not long.any():
            return long, 0

        def delayed(rate: float) -> np.ndarray:
            out = np.zeros(len(steps), dtype=bool)
            for index in np.flatnonzero(long):
                end = t[index + 1] + cfg.reception_window_s
                received = int(np.searchsorted(t, end, side="right")) - (index + 1)
                out[index] = received >= cfg.reception_min_fraction * rate * (end - t[index])
            return out

        total = t[-1] - t[0]
        late = delayed((len(t) - 1) / total) if total > 0 else np.zeros(len(steps), dtype=bool)
        real = long & ~late
        if real.any() and total - steps[real].sum() > 0:
            # Segunda pasada con la frecuencia medida fuera de los huecos reales.
            late = delayed((len(t) - 1 - int(real.sum())) / (total - steps[real].sum()))
            real = long & ~late
        return real, int((long & late).sum())

    def preparar(self, df: pd.DataFrame) -> PreparedSignal:
        """Valida el tiempo, mide la frecuencia efectiva y trata los huecos.

        Exige la columna ``_time``, ordena si hace falta y corta la serie en
        segmentos continuos donde el intervalo entre filas supera ``max_gap_s``.
        La frecuencia efectiva es el número de muestras por segundo de señal
        continua (tasa media): es robusta frente a marcas de tiempo que llegan
        en ráfagas, que se informan como irregularidad. Con ``time_grid:
        uniform`` las muestras de cada segmento se recolocan después en una
        rejilla de paso constante, anclada en la primera marca del segmento: no
        se interpola ni se añade o quita ninguna fila, solo se corrige el
        instante de cada muestra.

        Antes de cortar se separa la recepción retrasada de los huecos reales
        (:meth:`_real_gaps`). Los huecos se miden por grupo de sensores sobre
        los valores nuevos (:meth:`_fill_group`): en la presión siempre, y en
        la IMU solo con el pie en movimiento, porque quieto el valor repetido
        es una medida válida. La IMU de la serie es válida si, mientras el pie
        se mueve, repite valor en menos de ``imu_max_stale_moving`` de las
        filas. Nada se sustituye por cero.
        Por último estima la gravedad en reposo y añade la aceleración vertical
        ``Av`` y la velocidad de giro en torno a la vertical ``Wv``; si no hay
        reposo utilizable se usan ``Ay`` y ``Gy``.

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
        gaps, recovered = self._real_gaps(t, steps)
        segment = np.concatenate(([0], np.cumsum(gaps)))
        n_segments = int(segment[-1]) + 1
        inner = steps[~gaps]
        if len(inner) == 0 or inner.sum() <= 0:
            return PreparedSignal(error="ERR_TOO_FEW_SAMPLES")
        dt = float(inner.mean())
        fs = 1.0 / dt
        # Fracción de intervalos que se apartan más de un 50 % del medio (ráfagas).
        irregularity = float(np.mean(np.abs(inner - dt) > 0.5 * dt))
        gap_time = float(steps[gaps].sum())
        if cfg.time_grid == "uniform":
            # Las marcas son de recepción: dentro de cada segmento el instante real de la
            # muestra i es inicio + i·dt. Los valores y el número de filas no cambian.
            starts = np.flatnonzero(np.concatenate(([True], gaps)))
            counts = np.diff(np.append(starts, len(t)))
            anchors = t[starts].copy()
            for k in range(1, len(anchors)):  # un segmento nunca empieza antes de acabar el anterior
                anchors[k] = max(anchors[k], anchors[k - 1] + counts[k - 1] * dt)
            t = anchors[segment] + (np.arange(len(t)) - starts[segment]) * dt
        duration = float(t[-1] - t[0] + dt)
        if cfg.time_grid == "uniform":
            gap_time = max(0.0, duration - len(t) * dt)

        numeric = {
            channel: pd.to_numeric(frame[channel], errors="coerce").to_numpy(dtype=float)
            for channel in SIGNAL_CHANNELS if channel in frame.columns
        }
        direction, tilt = self._gravity(numeric)

        data = pd.DataFrame(index=np.arange(len(t)))
        coverage: Dict[str, float] = {}
        group_gaps: Dict[str, int] = {}
        interpolated = 0
        partial = PreparedSignal(t=t, data=data, segment=segment, dt=dt, fs=fs, report={"cobertura": coverage})

        # --- Presión: la regla de huecos se aplica siempre (varía incluso con el pie apoyado) ---
        present = [channel for channel in PRESSURE_CHANNELS if channel in numeric]
        if present:
            values, filled, n_gaps, _held = self._fill_group(np.column_stack([numeric[c] for c in present]), t, segment)
            interpolated += filled
            group_gaps["presion"] = n_gaps
            # Cobertura: fracción del tiempo de señal continua fuera de huecos.
            coverage["presion"] = round(float(1 - np.isnan(values).any(axis=1).mean()), 4)
            for position, channel in enumerate(present):
                data[channel] = values[:, position]
                coverage[channel] = coverage["presion"]

        # --- IMU: el pie se mueve si gira deprisa o si la presión cambia de estado cerca ---
        stale_moving, imu_valid = float("nan"), False
        present = [channel for channel in IMU_CHANNELS if channel in numeric]
        if present:
            raw = np.column_stack([numeric[c] for c in present])
            moving = np.zeros(len(t), dtype=bool)
            if all(channel in numeric for channel in ("Gx", "Gy", "Gz")):
                gyro_norm = np.linalg.norm(np.column_stack([numeric[c] for c in ("Gx", "Gy", "Gz")]), axis=1)
                fast = np.nan_to_num(gyro_norm, nan=0.0) > cfg.imu_moving_gyro
                # Muestras vecinas: una fila repetida entre dos giros rápidos también es movimiento.
                moving |= fast | np.concatenate(([False], fast[:-1])) | np.concatenate((fast[1:], [False]))
            if len(coverage) and all(channel in data.columns for channel in PRESSURE_CHANNELS):
                states, _status, reason = self._contact(partial)
                if not reason:
                    change = np.zeros(len(t), dtype=bool)
                    change[1:] = np.nan_to_num(np.diff(states["suelo"]), nan=0.0) != 0
                    reach = self.seconds_to_samples(cfg.max_gap_s, fs)
                    moving |= pd.Series(change).rolling(2 * reach + 1, center=True, min_periods=1).max().to_numpy().astype(bool)
            values, filled, n_gaps, held = self._fill_group(raw, t, segment, still=~moving)
            interpolated += filled
            group_gaps["imu"] = n_gaps
            coverage["imu"] = round(float(1 - np.isnan(values).any(axis=1).mean()), 4)
            stale_moving = float((held & moving).sum() / moving.sum()) if moving.any() else 0.0
            imu_valid = stale_moving <= cfg.imu_max_stale_moving
            for position, channel in enumerate(present):
                data[channel] = values[:, position]
                coverage[channel] = coverage["imu"]

        if all(channel in data.columns for channel in IMU_CHANNELS):
            if direction is not None:
                data[VERTICAL_ACCEL] = data[["Ax", "Ay", "Az"]].to_numpy() @ direction
                data[VERTICAL_RATE] = data[["Gx", "Gy", "Gz"]].to_numpy() @ direction
            else:
                data[VERTICAL_ACCEL], data[VERTICAL_RATE] = data["Ay"], data["Gy"]
            coverage[VERTICAL_ACCEL] = coverage[VERTICAL_RATE] = coverage["imu"]

        report = {
            "muestras": len(t), "fs_efectiva": round(fs, 3), "duracion_real_seg": round(duration, 3),
            "n_huecos": int(gaps.sum()), "tiempo_huecos_seg": round(gap_time, 3), "n_segmentos": n_segments,
            "intervalos_recuperados": recovered, "imu_valida": bool(imu_valid),
            "imu_repeticion_en_movimiento_prc": round(100 * stale_moving, 1),
            "exposicion_seg": round(duration - gap_time, 3), "cobertura": coverage, "huecos_por_grupo": group_gaps,
            "muestras_interpoladas": interpolated, "tiempos_invalidos": invalid_times,
            "tiempos_repetidos": int(repeated.sum()), "reordenada": bool(reordered),
            "irregularidad_tiempo": round(irregularity, 3), "intervalo_mediano_s": round(float(np.median(inner)), 5),
            "rejilla_tiempo": cfg.time_grid,
            "vertical_estimada": direction is not None, "inclinacion_grados": round(tilt, 1),
            "fs_desviada": bool(abs(fs - cfg.fs) / cfg.fs > cfg.fs_tolerance),
        }
        logger.debug(_("DEBUG_SIGNAL_PREPARED: {0} muestras, fs efectiva {1:.2f} Hz (nominal {2:g}), {3} huecos ({4:.2f} s), {5} muestras interpoladas, irregularidad temporal {6:.0%}").format(len(t), fs, cfg.fs, report['n_huecos'], gap_time, interpolated, irregularity))
        logger.debug(_("DEBUG_SIGNAL_GROUPS: cobertura IMU {0:.0%} ({1} huecos), cobertura presión {2:.0%} ({3} huecos), inclinación respecto a Ay {4:.0f} grados (vertical estimada: {5})").format(
            coverage.get("imu", float("nan")), group_gaps.get("imu", 0), coverage.get("presion", float("nan")),
            group_gaps.get("presion", 0), tilt, direction is not None))
        logger.debug(_("DEBUG_SIGNAL_QUALITY: IMU repetida en movimiento {0:.1f} % (válida: {1}); {2} intervalos largos recuperados como recepción retrasada").format(
            100 * stale_moving, imu_valid, recovered))
        prepared = PreparedSignal(t=t, data=data, segment=segment, dt=dt, fs=fs, report=report)
        if cfg.turn_smooth_cutoff_hz >= fs / 2:
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
        inertial = [c for c in channels if c in IMU_CHANNELS or c in (VERTICAL_ACCEL, VERTICAL_RATE)]
        if inertial and not prepared.report.get("imu_valida", False):
            # La IMU repite demasiados valores mientras el pie se mueve.
            return "ERR_IMU_STALE"
        low = [c for c in channels if c not in inertial and prepared.report["cobertura"][c] < self.config.min_coverage]
        if low:
            return f"ERR_LOW_COVERAGE_{','.join(low).upper()}"
        return ""

    def _turns(self, prepared: PreparedSignal, excluir_giros: bool) -> tuple[np.ndarray, Dict[str, Any], set[str]]:
        """Marca las muestras en giro por ángulo acumulado y resume los episodios.

        La velocidad de giro en torno a la vertical se suaviza para quitar la
        oscilación de cada zancada. Un episodio candidato tiene un núcleo por
        encima de ``turn_threshold`` y se extiende a ambos lados, mientras el
        giro conserve el sentido, hasta bajar de ``turn_extend_threshold``: con
        un solo umbral se pierde el ángulo del arranque y del final y se
        cuentan menos giros de los reales. Es un giro si el ángulo acumulado
        (integral de la velocidad en el episodio) alcanza ``turn_min_angle_deg``.

        Returns:
            tuple[np.ndarray, Dict[str, Any], set[str]]: Máscara de giro, resumen
            (número, tiempo real y ángulo de los giros, y episodios descartados)
            y métodos de filtrado aplicados.
        """
        cfg = self.config
        turning = np.zeros(len(prepared.t), dtype=bool)
        summary = {"tiempo_giros_seg": 0.0, "n_giros": 0, "n_giros_descartados": 0, "angulo_giros_grados": 0.0,
                   "giros_evaluados": False}
        channel = VERTICAL_RATE if cfg.turn_axis == "vertical" else cfg.turn_axis
        if not excluir_giros or self._usable(prepared, (channel,)):
            return turning, summary, set()
        rate, methods = self._filtered(prepared, channel, cfg.turn_smooth_cutoff_hz)
        speed = np.nan_to_num(np.abs(rate), nan=0.0)
        core = speed > cfg.turn_threshold
        angle_total, kept, candidates = 0.0, 0, 0
        episodes: list[Dict[str, float]] = []
        for start, stop in self._valid_runs(prepared, (channel,), speed > cfg.turn_extend_threshold):
            if not core[start:stop].any():
                continue
            candidates += 1
            # Tramo del episodio que gira en el mismo sentido que su punto de máxima velocidad.
            peak = start + int(np.argmax(speed[start:stop]))
            same = np.sign(rate[start:stop]) == np.sign(rate[peak])
            first, last = next((a + start, b + start) for a, b in _true_runs(same) if a + start <= peak < b + start)
            angle = abs(float(np.trapezoid(rate[first:last], prepared.t[first:last]))) if last - first > 1 else 0.0
            if angle >= cfg.turn_min_angle_deg and self._exposure(prepared, [(first, last)]) >= cfg.turn_min_duration_s:
                turning[first:last] = True
                angle_total += angle
                kept += 1
                episodes.append({"inicio_s": round(float(prepared.t[first]), 3), "fin_s": round(float(prepared.t[last - 1]), 3),
                                 "angulo_grados": round(angle, 1)})
        prepared.cache["giros"] = episodes
        summary = {
            "tiempo_giros_seg": round(self._exposure(prepared, _true_runs(turning)), 2), "n_giros": kept,
            "n_giros_descartados": candidates - kept, "angulo_giros_grados": round(angle_total, 1),
            "giros_evaluados": True,
        }
        return turning, summary, methods
        return turning, summary, methods

    def _contact(self, prepared: PreparedSignal) -> tuple[Dict[str, np.ndarray], str, str]:
        """Comprueba la calidad de la presión y clasifica cada muestra como cargada o descargada.

        Cada sensor se suaviza y se compara con el punto medio entre sus
        percentiles 5 y 95; la polaridad es ``pressure_stance_is_low``. El pie
        está en el suelo cuando algún sensor (talón o antepié) está cargado. Un
        sensor del antepié cuyo rango no llega a ``pressure_min_range`` es plano
        y no aporta información (hay pacientes que no cargan bien la punta).

        La calidad del talón se mide antes de detectar nada y no depende de la
        escala: debe tener modulación de marcha, es decir, una autocorrelación
        de al menos ``heel_min_periodicity`` en el periodo de zancada. Si el
        tramo continuo más largo dura menos de dos zancadas máximas no puede
        medirse el ritmo, y entonces se exige el rango mínimo. El resultado y
        el motivo de cada descarte quedan en el informe de la serie.

        Returns:
            tuple[Dict[str, np.ndarray], str, str]: Por sensor y en ``suelo``,
            vector con 1 (cargado), 0 (descargado) o ``NaN``; en ``talon_norm``,
            la carga del talón normalizada entre 0 y 1; estado y motivo si la
            presión no es utilizable.
        """
        if "contacto" in prepared.cache:
            return prepared.cache["contacto"]
        cfg = self.config
        result: tuple[Dict[str, np.ndarray], str, str]
        problem = self._usable(prepared, PRESSURE_CHANNELS)
        if problem:
            result = ({}, "ERROR", problem)
            prepared.cache["contacto"] = result
            return result
        window = self.seconds_to_samples(cfg.pressure_window_s, prepared.fs)
        runs = self._valid_runs(prepared, PRESSURE_CHANNELS)
        states: Dict[str, np.ndarray] = {}
        failure = ""
        for channel in PRESSURE_CHANNELS:
            raw = prepared.data[channel].to_numpy(dtype=float)
            smooth = np.full(len(raw), np.nan)
            for start, stop in runs:
                smooth[start:stop] = pd.Series(raw[start:stop]).rolling(window=window, center=True, min_periods=1).mean().to_numpy()
            valid = smooth[~np.isnan(smooth)]
            if len(valid) == 0:
                failure = "ERR_EMPTY_PRESSURE"
                break
            high, low = np.percentile(valid, 95), np.percentile(valid, 5)
            spread = float(high - low)
            if channel == cfg.heel_channel:
                # Ritmo de marcha del talón, medido en el tramo continuo más largo.
                longest = max(runs, key=lambda run: run[1] - run[0])
                span = (longest[1] - longest[0]) * prepared.dt
                periodicity = float("nan")
                if span >= 2 * cfg.cycle_max_s and spread > 0:
                    centred = smooth[longest[0]:longest[1]] - smooth[longest[0]:longest[1]].mean()
                    spectrum = np.fft.rfft(centred, n=2 * len(centred))
                    correlation = np.fft.irfft(spectrum * np.conj(spectrum))[:len(centred)]
                    if correlation[0] > 0:
                        first = self.seconds_to_samples(cfg.cycle_min_s, prepared.fs)
                        last = min(self.seconds_to_samples(cfg.cycle_max_s, prepared.fs), len(centred) - 1)
                        periodicity = float((correlation[first:last + 1] / correlation[0]).max())
                criterion = "periodicidad" if np.isfinite(periodicity) else "rango"
                prepared.report.update({
                    "rango_talon": round(spread, 2), "periodicidad_talon": round(periodicity, 3),
                    "criterio_presion": criterion,
                })
                if spread <= 0:
                    failure = "ERR_FLAT_PRESSURE: presión del talón sin variación"
                elif criterion == "periodicidad" and periodicity < cfg.heel_min_periodicity:
                    failure = "ERR_NO_GAIT_MODULATION: presión sin modulación de marcha"
                elif criterion == "rango" and spread < cfg.pressure_min_range:
                    failure = (f"ERR_SHORT_WINDOW: ventana demasiado corta para medir el ritmo y rango de presión "
                               f"menor de {cfg.pressure_min_range:g}")
                if failure:
                    break
                norm = np.clip((smooth - low) / spread, 0.0, 1.0)
                states["talon_norm"] = 1.0 - norm if cfg.pressure_stance_is_low else norm
            elif spread < cfg.pressure_min_range:
                continue  # sensor del antepié plano
            threshold = (high + low) / 2
            loaded = smooth < threshold if cfg.pressure_stance_is_low else smooth > threshold
            states[channel] = np.where(np.isnan(smooth), np.nan, loaded.astype(float))
        if failure:
            result = ({}, "ERROR" if failure == "ERR_EMPTY_PRESSURE" else "WARNING", failure)
        else:
            stacked = np.column_stack([states[channel] for channel in PRESSURE_CHANNELS if channel in states])
            states["suelo"] = np.where(np.isnan(stacked).any(axis=1), np.nan, np.nan_to_num(stacked).max(axis=1))
            result = (states, "OK", "")
        prepared.cache["contacto"] = result
        return result

    def _strides(self, prepared: PreparedSignal, states: Dict[str, np.ndarray], steady: np.ndarray) -> Dict[str, Any]:
        """Detecta los contactos de talón y forma las zancadas.

        Método de "valle + cruce de umbral" (el de ``_detect_gait_events`` del
        proyecto 2026_msFeat_TEA, adaptado a tramos continuos y a la polaridad
        configurable): primero se localizan los valles de mitad de balanceo,
        donde el talón está más descargado, separados al menos ``cycle_min_s``
        y con prominencia ``heel_valley_prominence``; el contacto de talón es
        el primer instante tras cada valle en el que el talón pasa a estar
        cargado. Así el ruido alrededor del umbral no genera contactos. Una
        zancada va de un contacto al siguiente del mismo tramo y es válida si
        su duración está entre ``cycle_min_s`` y ``cycle_max_s``.

        Returns:
            Dict[str, Any]: ``contactos`` (índices), ``zancadas`` (lista de
            ``(inicio, fin, duración, válida)``), ``tramos`` (número de tramos
            continuos analizados) y ``exposicion`` (segundos analizados).
        """
        cfg = self.config
        heel, load = states[cfg.heel_channel], states["talon_norm"]
        runs = self._valid_runs(prepared, PRESSURE_CHANNELS, steady)
        distance = self.seconds_to_samples(cfg.cycle_min_s, prepared.fs)
        contacts: list[int] = []
        strides: list[tuple[int, int, float, bool]] = []
        for start, stop in runs:
            valleys = find_peaks(-load[start:stop], distance=distance, prominence=cfg.heel_valley_prominence)[0] + start
            kept: list[int] = []
            for position, valley in enumerate(valleys):
                limit = valleys[position + 1] if position + 1 < len(valleys) else stop
                loaded = np.flatnonzero(heel[valley:limit] == 1)
                if len(loaded) == 0:
                    continue
                index = int(valley + loaded[0])
                # Tolerancia de media muestra para que el redondeo no descarte un contacto en el límite.
                if not kept or prepared.t[index] - prepared.t[kept[-1]] >= cfg.cycle_min_s - prepared.dt / 2:
                    kept.append(index)
            contacts.extend(kept)
            for first, second in zip(kept[:-1], kept[1:]):
                duration = float(prepared.t[second] - prepared.t[first])
                ok = cfg.cycle_min_s - prepared.dt / 2 <= duration <= cfg.cycle_max_s + prepared.dt / 2
                strides.append((first, second, duration, ok))
        return {"contactos": contacts, "zancadas": strides, "tramos": len(runs), "exposicion": self._exposure(prepared, runs)}

    # ------------------------------------------------------------------ métricas

    def calcular_cadencia(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True,
                          min_strides: int | None = None, skip_first: int = 0) -> Dict[str, Any]:
        """Cuenta las zancadas de un pie y calcula su cadencia a partir del contacto de talón.

        El detector principal es el contacto de talón en el sensor de presión
        del talón (``heel_channel``). La calidad de la señal se comprueba antes
        de detectar: cobertura del grupo de presión y sensor de talón no plano;
        si no la supera no se cuenta nada (no se cambia a otra modalidad por
        cuál detecta más). Los giros se retiran sin compactar la señal. La
        cadencia es 60 entre el tiempo medio de las zancadas válidas, y solo se
        informa con al menos ``min_strides``.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de una extremidad con
                ``_time`` y las señales, o la serie ya preparada.
            excluir_giros (bool, optional): Si es True, elimina del cálculo los
                intervalos asociados a giros. Por defecto es True.
            min_strides (int | None): Mínimo de zancadas válidas para informar
                la cadencia; ``None`` no exige mínimo.
            skip_first (int): Zancadas válidas iniciales que se descartan (arranque).

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'tiempos_zancada' (list[float]): Duración de cada zancada válida.
                - 'num_zancadas' (int): Zancadas válidas de este pie.
                - 'cadencia_zancada' (float): Zancadas por minuto de marcha.
                - 'contactos_talon' (int): Contactos de talón detectados.
                - 'tiempo_zancada_seg' (float): Tiempo medio de zancada válida.
                - 'tiempo_zancada_std_seg' (float): Variabilidad entre zancadas.
                - 'zancadas_en_banda_prc' (float): Porcentaje de zancadas con
                  duración dentro de la banda válida.
                - 'n_tramos' (int): Tramos continuos de marcha analizados.
                - 'tiempo_giros_seg', 'n_giros', 'angulo_giros_grados': Giros.
                - 'tiempo_analizado_seg' (float): Marcha estable analizada.
                - 'modalidad' (str): Señal usada por el detector.
                - 'fs_efectiva', 'duracion_real_seg', 'n_huecos': Calidad temporal.
                - 'status' (str): OK, WARNING o ERROR; 'error_reason' si no es OK.
        """
        cfg = self.config
        keys = ("num_zancadas", "cadencia_zancada", "tiempo_zancada_seg", "zancadas_en_banda_prc")
        minimum = 1 if min_strides is None else int(min_strides)
        prepared = self._prepared(df)
        if prepared.error:
            status = "WARNING" if prepared.error == "ERR_TOO_FEW_SAMPLES" else "ERROR"
            return self._failure(keys, status, prepared.error, prepared, tiempos_zancada=[])

        # Calidad de la señal, medida antes de detectar.
        states, status, reason = self._contact(prepared)
        if reason:
            return self._failure(keys, status, reason, prepared, modalidad=cfg.heel_channel, tiempos_zancada=[])

        turning, turns, _methods = self._turns(prepared, excluir_giros)
        found = self._strides(prepared, states, ~turning)
        valid = [duration for _first, _second, duration, ok in found["zancadas"] if ok][max(0, int(skip_first)):]
        analysed = found["exposicion"]
        in_band = 100.0 * len(valid) / len(found["zancadas"]) if found["zancadas"] else float("nan")
        common = {
            **turns, "tiempo_analizado_seg": round(analysed, 2), "modalidad": cfg.heel_channel,
            "contactos_talon": len(found["contactos"]), "n_tramos": found["tramos"],
            "zancadas_en_banda_prc": round(in_band, 1), "metodo_filtro": "media_movil",
            "exposicion_seg": prepared.report["exposicion_seg"], "tiempos_zancada": [round(value, 4) for value in valid],
            "min_zancadas": minimum, "contactos_s": [round(float(prepared.t[index]), 3) for index in found["contactos"]],
            "giros": prepared.cache.get("giros", []),
        }
        if turns["tiempo_giros_seg"] > 0 and analysed < cfg.min_steady_s:
            logger.debug(_("DEBUG_NO_STEADY_GAIT: {0:.2f} s de marcha estable tras {1} s de giros (mínimo {2} s); cadencia no calculada").format(analysed, turns['tiempo_giros_seg'], cfg.min_steady_s))
            return self._failure(keys, "WARNING", "ERR_NO_STEADY_GAIT", prepared, **common)
        if analysed <= 0:
            return self._failure(keys, "WARNING", "ERR_NO_VALID_SEGMENTS", prepared, **common)
        if len(valid) < minimum:
            return self._failure(keys, "WARNING", "ERR_TOO_FEW_STRIDES", prepared, **{**common, "num_zancadas": len(valid)})

        stride_time = float(np.mean(valid))
        cadencia = 60.0 / stride_time

        # --- Validación Biomecánica (Bloque 3) ---
        status = "OK" if cfg.cadence_min <= cadencia <= cfg.cadence_max else "WARNING"
        return {
            "num_zancadas": len(valid), "cadencia_zancada": round(cadencia, 2), "tiempo_zancada_seg": round(stride_time, 3),
            "tiempo_zancada_std_seg": round(float(np.std(valid)), 3),
            **common, **self._quality(prepared),
            "status": status, "error_reason": "" if status == "OK" else "WARN_CADENCE_OUT_OF_BOUNDS",
        }

    def calcular_fases(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True,
                       min_strides: int | None = None, skip_first: int = 0) -> Dict[str, Any]:
        """Determina la distribución porcentual de las fases de apoyo (Stance)
        y balanceo (Swing) del ciclo de marcha.

        Cada ciclo es una zancada válida (de un contacto de talón al siguiente,
        en marcha estable). El balanceo es el tiempo del ciclo en el que ningún
        sensor de presión está cargado. Un ciclo solo cuenta si su balanceo
        dura entre ``swing_min_s`` y ``swing_max_s`` y ocupa entre
        ``swing_min_prc`` y ``swing_max_prc`` del ciclo. El reposo, los giros y
        los ciclos incompletos quedan fuera.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie del lado correspondiente
                con ``_time``, 'S0', 'S1' y 'S2', o la serie ya preparada.
            excluir_giros (bool, optional): Excluir los ciclos en giro.
            min_strides (int | None): Mínimo de ciclos válidos; ``None`` no exige mínimo.
            skip_first (int): Ciclos válidos iniciales que se descartan (arranque).

        Returns:
            Dict[str, Any]: Distribución de fases:
                - 'balanceos_ciclo_prc' (list[float]): Balanceo de cada ciclo válido.
                - 'apoyo_prc' (float): Media del porcentaje de apoyo por ciclo.
                - 'balanceo_prc' (float): Porcentaje de balanceo.
                - 'n_ciclos' (int), 'n_ciclos_excluidos' (int): Ciclos usados y descartados.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("apoyo_prc", "balanceo_prc")
        minimum = 1 if min_strides is None else int(min_strides)
        prepared = self._prepared(df)
        if prepared.error:
            return self._failure(keys, "ERROR", prepared.error, prepared, balanceos_ciclo_prc=[])
        states, status, reason = self._contact(prepared)
        if reason:
            return self._failure(keys, status, reason, prepared, balanceos_ciclo_prc=[])

        turning, _turns, _methods = self._turns(prepared, excluir_giros)
        found = self._strides(prepared, states, ~turning)
        ground = states["suelo"]
        swing_pct: list[float] = []
        excluded = 0
        tolerance = prepared.dt / 2
        for first, second, duration, ok in found["zancadas"]:
            swing = float((ground[first:second] == 0).sum() * prepared.dt)
            share = 100.0 * swing / duration if duration > 0 else float("nan")
            if not ok or not cfg.swing_min_s - tolerance <= swing <= cfg.swing_max_s + tolerance \
                    or not cfg.swing_min_prc <= share <= cfg.swing_max_prc:
                excluded += 1
                continue
            swing_pct.append(share)
        swing_pct = swing_pct[max(0, int(skip_first)):]
        shares = [round(value, 3) for value in swing_pct]
        if len(swing_pct) < minimum:
            reason = "ERR_NO_VALID_CYCLES" if not swing_pct else "ERR_TOO_FEW_STRIDES"
            return self._failure(keys, "WARNING", reason, prepared, n_ciclos=len(swing_pct), n_ciclos_excluidos=excluded,
                                 balanceos_ciclo_prc=shares)

        balanceo = float(np.mean(swing_pct))
        return {
            "apoyo_prc": round(100.0 - balanceo, 2), "balanceo_prc": round(balanceo, 2),
            "apoyo_std_prc": round(float(np.std(swing_pct)), 2),
            "n_ciclos": len(swing_pct), "n_ciclos_excluidos": excluded, "metodo_filtro": "media_movil",
            "balanceos_ciclo_prc": shares, **self._quality(prepared), "status": "OK", "error_reason": "",
        }

    def calcular_decaimiento_6mwt(
        self, df: pd.DataFrame, duracion_registro_s: float | None = None, inicio: Any = None
    ) -> Dict[str, Any]:
        """Calcula el índice de fatiga o decaimiento de la cadencia a lo largo del test 6MWT.

        El ensayo se divide en minutos (bloques de ``decay_block_s``) contados
        desde el inicio de la ventana del registro. Un minuto es válido si
        tiene datos de presión utilizables durante al menos ``min_coverage`` de
        su duración. El 6MWT es completo si la ventana del registro dura
        ``sixmwt_duration_s`` ± ``sixmwt_tolerance_s`` y hay al menos
        ``sixmwt_min_valid_minutes`` minutos válidos. El decaimiento compara la
        cadencia del primer minuto con la del último y solo se calcula si el
        ensayo es completo, ambos están entre los válidos y cada uno tiene al
        menos ``min_strides_minute`` zancadas válidas; en otro caso queda vacío
        con su motivo.

        Args:
            df (pd.DataFrame): Datos del test de marcha de 6 minutos con ``_time``.
            duracion_registro_s (float | None): Duración de la ventana del
                registro; ``None`` usa la duración de la serie.
            inicio (Any): Inicio de la ventana del registro, en el mismo reloj
                que ``_time``; ``None`` usa la primera muestra.

        Returns:
            Dict[str, Any]: Fatiga cuantificada:
                - '6mwt_decaimiento' (float): Porcentaje de pérdida de cadencia.
                - 'cadencia_inicial', 'cadencia_final' (float): Cadencia del
                  primer y del último minuto.
                - 'minutos_validos' (int): Minutos con datos válidos.
                - 'ensayo_completo' (bool): Si se acepta como 6MWT completo.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("6mwt_decaimiento",)
        prepared = self.preparar(df)
        if prepared.error:
            return self._failure(keys, "WARNING", prepared.error, prepared, minutos_validos=0, ensayo_completo=False)

        # Segundos de cada muestra desde el inicio de la ventana del registro.
        first_sample = pd.to_datetime(df["_time"], errors="coerce").min()
        offset = 0.0 if inicio is None else float((first_sample - pd.Timestamp(inicio)).total_seconds())
        elapsed = prepared.t + offset
        registered = prepared.report["duracion_real_seg"] if duracion_registro_s is None else float(duracion_registro_s)
        n_blocks = int(round(cfg.sixmwt_duration_s / cfg.decay_block_s))
        block_of = np.floor(elapsed / cfg.decay_block_s).astype(int)

        # Minutos con datos válidos: presión utilizable durante la fracción mínima del minuto.
        has_pressure = all(channel in prepared.data.columns for channel in PRESSURE_CHANNELS)
        usable = prepared.data[list(PRESSURE_CHANNELS)].notna().all(axis=1).to_numpy() if has_pressure else np.zeros(len(elapsed), dtype=bool)
        valid_blocks = [
            block for block in range(n_blocks)
            if (usable & (block_of == block)).sum() * prepared.dt >= cfg.min_coverage * cfg.decay_block_s
        ]
        valid_minutes = len(valid_blocks)
        window_ok = abs(registered - cfg.sixmwt_duration_s) <= cfg.sixmwt_tolerance_s
        complete = window_ok and valid_minutes >= cfg.sixmwt_min_valid_minutes
        extra = {"minutos_validos": valid_minutes, "ensayo_completo": complete}
        logger.debug(_("DEBUG_6MWT_MINUTES: ventana de {0:.0f} s, {1} de {2} minutos válidos, completo: {3}").format(registered, valid_minutes, n_blocks, complete))
        if not window_ok:
            reason = (f"ERR_6MWT_VENTANA: 6MWT incompleto: la ventana del registro dura {registered:.0f} s "
                      f"(se esperan {cfg.sixmwt_duration_s:.0f} ± {cfg.sixmwt_tolerance_s:.0f} s)")
            return self._failure(keys, "WARNING", reason, prepared, **extra)
        if not complete or 0 not in valid_blocks or n_blocks - 1 not in valid_blocks:
            missing = [name for name, block in (("el primero", 0), ("el último", n_blocks - 1)) if block not in valid_blocks]
            detail = f" (falta {' y '.join(missing)})" if missing and valid_minutes >= cfg.sixmwt_min_valid_minutes else ""
            reason = f"ERR_6MWT_INCOMPLETO: 6MWT incompleto: {valid_minutes} minutos válidos{detail}"
            return self._failure(keys, "WARNING", reason, prepared, **extra)

        # Las zancadas se detectan una sola vez sobre el ensayo entero y se reparten por minutos.
        states, status, reason = self._contact(prepared)
        if reason:
            return self._failure(keys, status, reason, prepared, **extra)
        turning, _turns, _methods = self._turns(prepared, True)
        found = self._strides(prepared, states, ~turning)
        cadences = {}
        for name, block in (("inicial", 0), ("final", n_blocks - 1)):
            durations = [duration for first, _second, duration, ok in found["zancadas"] if ok and block_of[first] == block]
            if len(durations) < cfg.min_strides_minute:
                return self._failure(keys, "WARNING", f"ERR_6MWT_SIN_CADENCIA: sin zancadas válidas suficientes en el minuto {name}", prepared, **extra)
            cadences[name] = 60.0 / float(np.mean(durations))

        decay = (cadences["inicial"] - cadences["final"]) / cadences["inicial"] * 100
        return {
            "6mwt_decaimiento": round(decay, 2), "cadencia_inicial": round(cadences["inicial"], 2),
            "cadencia_final": round(cadences["final"], 2), **extra,
            **self._quality(prepared), "status": "OK", "error_reason": "",
        }

    def _integrate_stride(self, acc: np.ndarray, gyr: np.ndarray, t: np.ndarray) -> np.ndarray:
        """Integra el movimiento del sensor entre dos pies planos consecutivos.

        Marco global con el eje vertical alineado con la gravedad. En el primer
        instante el pie está plano y quieto: la aceleración medida es la
        gravedad y fija la orientación del sensor (recalibración en cada
        zancada). Desde ahí se integra el giroscopio para seguir la orientación,
        se lleva la aceleración al marco global, se le resta la gravedad y se
        integra dos veces. Condiciones de contorno en cada pie plano: velocidad
        nula (ZUPT) y altura nula (nivel del suelo); la deriva se corrige de
        forma lineal entre los dos pies planos para que ambas acaben en cero.

        Args:
            acc (np.ndarray): Aceleración en g, filas × 3, en ejes del sensor.
            gyr (np.ndarray): Velocidad angular en grados/s, filas × 3.
            t (np.ndarray): Instante de cada fila en segundos.

        Returns:
            np.ndarray: Posición del sensor en metros, filas × 3 (x, y
            horizontales; z vertical, cero en los dos extremos).
        """
        up = acc[0] / np.linalg.norm(acc[0])
        target = np.array([0.0, 0.0, 1.0])
        axis = np.cross(up, target)
        sine, cosine = float(np.linalg.norm(axis)), float(up @ target)
        rotation = np.eye(3)
        if sine > 1e-12:
            k = axis / sine
            skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
            rotation = np.eye(3) + sine * skew + (1 - cosine) * (skew @ skew)
        elif cosine < 0:
            rotation = np.diag([1.0, -1.0, -1.0])
        world = np.empty_like(acc)
        omega = np.radians(gyr)
        for index in range(len(t)):
            world[index] = rotation @ acc[index]
            if index + 1 < len(t):
                step = (omega[index] + omega[index + 1]) / 2 * (t[index + 1] - t[index])
                angle = float(np.linalg.norm(step))
                if angle > 1e-12:
                    k = step / angle
                    skew = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
                    rotation = rotation @ (np.eye(3) + np.sin(angle) * skew + (1 - np.cos(angle)) * (skew @ skew))
        progress = (t - t[0]) / (t[-1] - t[0])
        linear = (world - target) * self.config.gravity
        velocity = cumulative_trapezoid(linear, x=t, axis=0, initial=0)
        velocity -= np.outer(progress, velocity[-1])          # velocidad nula en el pie plano final
        position = cumulative_trapezoid(velocity, x=t, axis=0, initial=0)
        position[:, 2] -= progress * position[-1, 2]          # altura nula en el pie plano final
        return position

    def _integrated_strides(self, prepared: PreparedSignal, excluir_giros: bool) -> tuple[list[Dict[str, Any]], str, str]:
        """Integra cada zancada entre pies planos y devuelve su longitud y sus alturas.

        Cada zancada va del instante de menor giro de un apoyo (pie plano y
        quieto) al del apoyo siguiente. Solo se integran zancadas con la IMU y
        la presión válidas de principio a fin, fuera de giros y con duración
        dentro de la banda de zancada. El módulo va en la caña del calcetín,
        sobre el tobillo: la trayectoria vertical integrada es la del tobillo y
        su máximo en el balanceo es la excursión vertical del tobillo. La
        distancia mínima punta-suelo (MTC) exige un sensor en el antepié o
        trasladar la trayectoria a la punta, así que queda vacía con su motivo.

        Returns:
            tuple[list[Dict[str, Any]], str, str]: Un registro por zancada
            (inicio, duración, longitud, excursión vertical del tobillo, MTC
            vacía y causas de exclusión), estado y motivo si no puede integrarse nada.
        """
        key = ("zancadas_integradas", bool(excluir_giros))
        if key in prepared.cache:
            return prepared.cache[key]
        cfg = self.config
        problem = self._usable(prepared, IMU_CHANNELS)
        if problem:
            return [], "ERROR", problem
        states, status, reason = self._contact(prepared)
        if reason:
            return [], status, reason

        turning, _turns, _methods = self._turns(prepared, excluir_giros)
        acc = prepared.data[["Ax", "Ay", "Az"]].to_numpy(dtype=float)
        gyr = prepared.data[["Gx", "Gy", "Gz"]].to_numpy(dtype=float)
        gyro_norm = np.linalg.norm(gyr, axis=1)
        ground = states["suelo"]
        tolerance = prepared.dt / 2
        events: list[Dict[str, Any]] = []
        for start, stop in self._valid_runs(prepared, (*IMU_CHANNELS, *PRESSURE_CHANNELS)):
            # Instante de pie plano de cada apoyo: el de menor giro.
            flats = [a + start + int(np.argmin(gyro_norm[a + start:b + start])) for a, b in _true_runs(ground[start:stop] == 1)]
            for first, second in zip(flats[:-1], flats[1:]):
                duration = float(prepared.t[second] - prepared.t[first])
                event = {
                    "inicio_s": round(float(prepared.t[first]), 3), "duracion_s": round(duration, 3),
                    "longitud_cm": float("nan"), "excursion_tobillo_cm": float("nan"), "altura_minima_cm": float("nan"),
                    "motivo_exclusion": "", "motivo_altura": MTC_NOT_COMPUTABLE,
                }
                events.append(event)
                if turning[first:second + 1].any():
                    event["motivo_exclusion"] = "en_giro"
                    continue
                if not cfg.cycle_min_s - tolerance <= duration <= cfg.cycle_max_s + tolerance:
                    event["motivo_exclusion"] = "duracion_fuera_de_rango"
                    continue
                position = self._integrate_stride(acc[first:second + 1], gyr[first:second + 1], prepared.t[first:second + 1])
                event["longitud_cm"] = round(float(np.hypot(position[-1, 0], position[-1, 1])) * 100, 2)
                swings = _true_runs(ground[first:second + 1] == 0)
                if swings:
                    a, b = max(swings, key=lambda run: run[1] - run[0])
                    event["excursion_tobillo_cm"] = round(float(position[a:b, 2].max()) * 100, 2)
        prepared.cache[key] = (events, "OK", "")
        return prepared.cache[key]

    def calcular_longitud_zancada(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True,
                                  min_strides: int | None = None, skip_first: int = 0) -> Dict[str, Any]:
        """Calcula la longitud de zancada media, en centímetros, por integración entre apoyos.

        Método de integración del movimiento del pie con recalibración de la
        posición del sensor en cada zancada (Hamacher D, Hamacher D, Taylor WR,
        Singh NB, Schega L. Towards clinical application: repetitive sensor
        position re-calibration for improved reliability of gait parameters.
        Gait Posture. 2014;39(4):1146-8. doi:10.1016/j.gaitpost.2014.01.020).
        La longitud sale directamente en metros, sin constante que calibrar ni
        distancia o talla de referencia; ver :meth:`_integrated_strides`. Los
        valores no están validados con una medida de referencia.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de la extremidad con
                ``_time``, la IMU y la presión, o la serie ya preparada.
            excluir_giros (bool, optional): Excluir las zancadas en giro.
            min_strides (int | None): Mínimo de zancadas integradas; ``None``
                no exige mínimo.
            skip_first (int): Zancadas integradas iniciales que se descartan (arranque).

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'stride_length_cm' (float): Longitud media de las zancadas integradas.
                - 'stride_length_std_cm' (float): Desviación típica entre zancadas.
                - 'n_zancadas_longitud' (int), 'n_zancadas_longitud_excluidas' (int).
                - 'metodo' (str): ``integracion_entre_apoyos``.
                - 'status' (str) y 'error_reason' (str).
        """
        cfg = self.config
        keys = ("stride_length_cm", "stride_length_std_cm")
        method = {"metodo": "integracion_entre_apoyos", "metodo_filtro": "sin_filtro"}
        minimum = 1 if min_strides is None else int(min_strides)
        prepared = self._prepared(df)
        if prepared.error:
            status = "WARNING" if prepared.error == "ERR_TOO_FEW_SAMPLES" else "ERROR"
            return self._failure(keys, status, prepared.error, prepared, **method)
        events, status, reason = self._integrated_strides(prepared, excluir_giros)
        if reason:
            return self._failure(keys, status, reason, prepared, **method)

        lengths = [event["longitud_cm"] for event in events if not event["motivo_exclusion"]][max(0, int(skip_first)):]
        counts = {"n_zancadas_longitud": len(lengths), "n_zancadas_longitud_excluidas": len(events) - len(lengths)}
        if len(lengths) < minimum:
            return self._failure(keys, "WARNING", "ERR_TOO_FEW_STRIDES", prepared, **method, **counts)

        mean_cm = round(float(np.mean(lengths)), 2)
        status = "OK" if cfg.stride_ok_min_cm <= mean_cm <= cfg.stride_ok_max_cm else "WARNING"
        return {
            "stride_length_cm": mean_cm, "stride_length_std_cm": round(float(np.std(lengths)), 2), **counts, **method,
            **self._quality(prepared), "status": status, "error_reason": "" if status == "OK" else "WARN_STRIDE_OUT_OF_BOUNDS",
        }

    def calcular_altura_zancada(self, df: pd.DataFrame | PreparedSignal, excluir_giros: bool = True,
                                min_strides: int | None = None, skip_first: int = 0) -> Dict[str, Any]:
        """Calcula la excursión vertical del tobillo en el balanceo; la MTC queda vacía.

        La distancia mínima punta-suelo (MTC, Müller R, Hamacher D, Hansen S,
        Oschmann P, Keune PM. Wearable inertial sensors are highly sensitive in
        the detection of gait disturbances and fatigue at early stages of
        multiple sclerosis. BMC Neurol. 2021;21:337.
        doi:10.1186/s12883-021-02361-y) se mide allí con el sensor en el
        antepié. Aquí el módulo va en la caña del calcetín, sobre el tobillo:
        la trayectoria integrada es la del tobillo y, sin trasladarla a la
        punta, la MTC no puede calcularse. Por eso ``altura_minima_cm`` se
        devuelve vacía con su motivo y las bandas de la MTC (``mtc_ok_*``) no
        se aplican a ninguna otra magnitud.

        Lo que sí se informa es la excursión vertical del tobillo: el máximo de
        su trayectoria vertical en el balanceo de cada zancada, con la misma
        integración entre pies planos que la longitud
        (:meth:`_integrate_stride`), y su variabilidad entre zancadas. No tiene
        banda de plausibilidad ni señal de referencia.

        Args:
            df (pd.DataFrame | PreparedSignal): Serie de una extremidad con
                ``_time``, la IMU y 'S0', 'S1' y 'S2', o la serie ya preparada.
            excluir_giros (bool, optional): Excluir las zancadas en giro.
            min_strides (int | None): Mínimo de zancadas integradas; ``None``
                no exige mínimo.
            skip_first (int): Zancadas integradas iniciales que se descartan (arranque).

        Returns:
            Dict[str, Any]: Diccionario con las métricas calculadas:
                - 'excursion_tobillo_cm' (float): Media de la excursión por zancada.
                - 'excursion_tobillo_std_cm' (float): Variabilidad entre zancadas.
                - 'altura_minima_cm' (float): MTC; siempre ``NaN``.
                - 'motivo_mtc' (str): Por qué la MTC no se calcula.
                - 'n_zancadas_altura' (int), 'n_zancadas_altura_excluidas' (int).
                - 'eventos' (list[dict]): Un registro por zancada con inicio,
                  duración, longitud, excursión y causas de exclusión.
                - 'status' (str) y 'error_reason' (str).
        """
        keys = ("excursion_tobillo_cm", "excursion_tobillo_std_cm", "altura_minima_cm")
        method = {"metodo": "integracion_entre_apoyos", "metodo_filtro": "sin_filtro", "motivo_mtc": MTC_NOT_COMPUTABLE}
        minimum = 1 if min_strides is None else int(min_strides)
        prepared = self._prepared(df)
        if prepared.error:
            return self._failure(keys, "ERROR", prepared.error, prepared, eventos=[], **method)
        events, status, reason = self._integrated_strides(prepared, excluir_giros)
        if reason:
            return self._failure(keys, status, reason, prepared, eventos=[], **method)

        values = [event["excursion_tobillo_cm"] for event in events if np.isfinite(event["excursion_tobillo_cm"])]
        values = values[max(0, int(skip_first)):]
        counts = {"n_zancadas_altura": len(values), "n_zancadas_altura_excluidas": len(events) - len(values)}
        if len(values) < minimum:
            reason = "ERR_NO_VALID_SWING_PHASES" if not values else "ERR_TOO_FEW_STRIDES"
            return self._failure(keys, "WARNING", reason, prepared, eventos=events, **method, **counts)
        return {
            "excursion_tobillo_cm": round(float(np.mean(values)), 2), "excursion_tobillo_std_cm": round(float(np.std(values)), 2),
            "altura_minima_cm": float("nan"), **counts, "eventos": events, **method, **self._quality(prepared),
            "status": "OK", "error_reason": "",
        }
