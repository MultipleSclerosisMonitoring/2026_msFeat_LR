import pandas as pd
import numpy as np

from datetime import timezone
from pandas import ExcelWriter
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

from msTools.data_manager import DataManager
from msTools import i18n
from msTools.timeutils import influx_wallclock_z
from msTools.flux import flux_range, flux_string
from msGait.models import EffectiveMovement

from scipy.signal import welch
from pydantic import ValidationError


class MovementDetector:
    """Detects effective movement and gait periods using raw sensor data from a data manager."""

    def __init__(
        self,
        config_file: str,
        sampling_rate: float,
        sect: str = "movement",
        fstart: Optional[str] = None,
        fend: Optional[str] = None,
        ids: Optional[List[int]] = None,
        ids_from_time: list[int] | None = None,
        verbose: int = 1
    ) -> None:
        """Initializes the movement detector, loads configurations and activity data.

        Args:
            config_file (str): Path to the YAML configuration file.
            sampling_rate (float): Sampling rate of the sensor data (in Hz).
            sect (str): Section in the YAML config for movement detection parameters.
            fstart (Optional[str]): Optional start timestamp for activity query.
            fend (Optional[str]): Optional end timestamp for activity query.
            ids (Optional[List[int]]): Optional list of segment IDs to retrieve.
            verbose (int): Verbosity level for logging (0 = silent, 1 = info, 2 = debug).
        """
        self.verbose = verbose
        self.sampling_rate = sampling_rate
        self.ids = ids
        # Initialize DataManager
        self.data_manager = DataManager(config_path=config_file, ids=self.ids)
        
        if ids is None and fstart is not None and fend is not None:
            """If only a time window is provided, resolve IDs from ``activity_all`` first.

            This switches the retrieval mode to ID-based when matches are found, ensuring
            deterministic segment selection for downstream steps.

            Args:
                fstart: Start of the time window (ISO str or datetime).
                fend: End of the time window (ISO str or datetime).
                ids: Optional explicit IDs (skips resolution when provided).
            """
            try:
                ids_from_time = self.data_manager.get_activity_ids_by_start_date_range(fstart, fend)
                # La selección de datos no depende del nivel de información.
                if ids_from_time:
                    self.ids = ids_from_time
                if verbose >= 1:
                    if ids_from_time:
                        print(f"[MovementDetector] {len(ids_from_time)} ID(s) found in activity_all for the time window.")
                    else:
                        print("[MovementDetector] No IDs found in activity_all for the requested time window.")
            except Exception as e:
                if verbose >= 1:
                    print(f"[MovementDetector] ID lookup by start_date failed: {e}")
        else:
            self.ids = ids

        # Retrieve activity segments based on IDs or time range from activity_all table
        self.activity_all = self.data_manager.segments_retrieval(
            fstart=fstart, fend=fend, ids=self.ids, verbose=verbose
        )
        if self.activity_all.empty and verbose >= 1:
            print(i18n._("FGAIT_NO_WINS"))

        # Retrieve full sensor-linked activity
        self.df_legs = self.data_manager.recover_activity_all(
            self.activity_all, vb=verbose
        )
        
        # Load detection parameters from config
        self._load_parameters(self.data_manager.get_config(sect), sect)

    def _load_parameters(self, params: dict, sect: str = "movement") -> None:
        """Reads the detection parameters from the configuration section.

        Args:
            params (dict): Content of the configuration section.
            sect (str): Section name, used in error messages.

        Raises:
            ValueError: If a required parameter is missing or a value is not valid.
        """
        params = params or {}
        for required in ("freq_band_min", "freq_band_max", "min_continuous_seconds"):
            if required not in params:
                raise ValueError(f"Falta {sect}.{required} en la configuración.")
        self.freq_band = (float(params["freq_band_min"]), float(params["freq_band_max"]))
        # Operational value: shortest sustained activity (seconds) accepted as effective movement.
        self.min_continuous_seconds = float(params["min_continuous_seconds"])
        # Pauses shorter than this stay inside an active streak. By default, the longest
        # period of the analysed band: movement at band frequencies cannot pause longer.
        self.max_pause_s = float(params.get("max_pause_s", 1.0 / self.freq_band[0]))
        # Two effective segments are joined only across gaps up to this long (see merge_connected_segments).
        self.merge_gap_s = float(params.get("merge_gap_s", 10.0))
        # Relative deviation between measured and nominal sampling rate that triggers a warning.
        self.fs_tolerance = float(params.get("fs_tolerance", 0.10))
        # Last, incomplete window: "align" (window ending at the last sample, overlapping the
        # previous one) or "minimum" (analysed as it is if it has tail_min_samples).
        self.tail_policy = params.get("tail_policy", "align")
        self.tail_min_samples = params.get("tail_min_samples")
        if self.tail_policy not in ("align", "minimum"):
            raise ValueError(f"{sect}.tail_policy debe ser align o minimum.")
        if self.tail_policy == "minimum" and self.tail_min_samples is None:
            raise ValueError(f"{sect}.tail_policy: minimum exige {sect}.tail_min_samples.")
        self.accel_threshold = params.get("accel_threshold", 0.2)
        self.gyro_threshold = params.get("gyro_threshold", 50)
        # Band power integrated over frequency (PSD x Hz). Defaults are the former
        # bin-sum thresholds (0.1 and 1000) times the former resolution, 50 Hz / 256.
        self.accel_power_threshold = params.get("accel_power_threshold", 0.1 * 50 / 256)
        self.gyro_power_threshold = params.get("gyro_power_threshold", 1000 * 50 / 256)

    def close(self):
        """Closing all the opened connections"""
        self.data_manager.close_all()
        
    def fetch_sensor_data(self, start_time: str, end_time: str,
                          codeid_id: int, foot: str) -> pd.DataFrame:
        """Fetches raw sensor data from InfluxDB for a specific time interval and limb.

        Args:
            start_time (str): Start time in ISO format.
            end_time (str): End time in ISO format.
            codeid_id (int): Identifier to map to real CodeID.
            foot (str): 'Left' or 'Right'.

        Returns:
            pd.DataFrame: Sensor data with fields Ax, Ay, Az, Gx, Gy, Gz, and timestamps.
        """
        try:
            codeid = self.data_manager.get_real_codeid(codeid_id)
        except ValueError as e:
            if self.verbose >= 1:
                print(i18n._("PGSQL-QRY-GEN-ERR").format(e=e))
            return pd.DataFrame()

        start_time = influx_wallclock_z(start_time)
        end_time = influx_wallclock_z(end_time)

        query = f'''
        from(bucket: {flux_string(self.data_manager.bucket)})
            |> range({flux_range(start_time, end_time)})
            |> filter(fn: (r) => r["CodeID"] == {flux_string(codeid)} and r["Foot"] == {flux_string(foot)})
            |> filter(fn: (r) => r["_field"] == "Ax" or r["_field"] == "Ay" or r["_field"] == "Az" 
                              or r["_field"] == "Gx" or r["_field"] == "Gy" or r["_field"] == "Gz")
            |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
        '''

        try:
            result = self.data_manager.influxdb_client.query_api().query(
                query=query,
                org=self.data_manager.config['influxdb']['org']
            )
            data = [record.values for table in result for record in table.records]
            return pd.DataFrame(data)
        except Exception as e:
            msg = str(e)
            if "cannot query an empty range" in msg:
                if self.verbose >= 2:
                    print(i18n._("WARN_EMPTY_RANGE").format(codeid=codeid, foot=foot))
                return pd.DataFrame()
            if self.verbose >= 1:
                print(i18n._("INFL-QRY-DATA-ERR").format(e=e))
            return pd.DataFrame()

    def calculate_magnitude(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculates signal magnitudes for acceleration and gyroscope data.

        Args:
            df (pd.DataFrame): DataFrame containing raw sensor values.

        Returns:
            pd.DataFrame: Same DataFrame with added ``|a|`` and ``|g|`` columns.
        """
        if {"Ax", "Ay", "Az", "Gx", "Gy", "Gz"}.issubset(df.columns):
            df["|a|"] = np.sqrt(df["Ax"]**2 + df["Ay"]**2 + df["Az"]**2)
            df["|g|"] = np.sqrt(df["Gx"]**2 + df["Gy"]**2 + df["Gz"]**2)
        else:
            if self.verbose >= 1:
                print(i18n._("MVDET-AG-NODAT"))
        return df

    @staticmethod
    def _seconds(times) -> np.ndarray:
        """Converts timestamps to seconds from the first one."""
        times = pd.to_datetime(pd.Series(np.asarray(times)))
        return (times - times.iloc[0]).dt.total_seconds().to_numpy() if len(times) else np.array([])

    def estimate_rate(self, times) -> float:
        """Measures the sampling rate from the timestamps.

        Intervals longer than ``max_pause_s`` are data gaps and are left out, so
        the result is the number of samples per second of continuous signal.

        Args:
            times: Sample timestamps (datetime-like), sorted.

        Returns:
            float: Measured rate in Hz, or ``nan`` if it cannot be measured.
        """
        steps = np.diff(self._seconds(times))
        steps = steps[(steps >= 0) & (steps <= self.max_pause_s)]
        if len(steps) == 0 or steps.sum() <= 0:
            return float("nan")
        return float(len(steps) / steps.sum())

    def is_effective_by_welch(self, signal: np.ndarray, power_threshold, fs: Optional[float] = None) -> bool:
        """Determines if a signal contains sufficient power in a frequency band.

        The band power is the PSD integrated over frequency (sum of the bins
        times the frequency resolution), so it does not depend on the number of
        samples per Welch segment.

        Args:
            signal (np.ndarray): Input signal array.
            power_threshold (float): Minimum integrated band power.
            fs (Optional[float]): Measured sampling rate of this signal; the
                nominal ``sampling_rate`` is used when it is not given or not finite.

        Returns:
            bool: True if power within the target frequency band reaches the threshold.
        """
        signal = np.asarray(signal, dtype=float)
        if signal.size < 2:
            return False
        rate = fs if fs is not None and np.isfinite(fs) and fs > 0 else self.sampling_rate
        freqs, power = welch(signal, fs=rate, nperseg=min(256, signal.size))
        resolution = freqs[1] - freqs[0]
        band_power = power[(freqs >= self.freq_band[0]) & (freqs <= self.freq_band[1])].sum() * resolution
        return bool(band_power >= power_threshold)

    def active_streaks(self, signal: np.ndarray, threshold: float, times=None) -> np.ndarray:
        """Marks the samples that belong to a sustained active streak.

        A sample is active when ``|signal|`` exceeds the threshold. Consecutive
        active samples separated by at most ``max_pause_s`` (measured on the
        timestamps) form one streak, so the dips of an oscillating movement do
        not break it but a pause or a data gap does. A streak counts only if it
        lasts at least ``min_continuous_seconds``.

        Args:
            signal (np.ndarray): Input signal array.
            threshold (float): Activity threshold.
            times: Sample timestamps; without them the samples are assumed
                evenly spaced at the nominal ``sampling_rate``.

        Returns:
            np.ndarray: Boolean mask, True from the first to the last active
            sample of every streak that is long enough.
        """
        signal = np.asarray(signal, dtype=float)
        mask = np.zeros(signal.size, dtype=bool)
        if signal.size == 0 or np.std(signal) < 0.01:
            return mask
        seconds = np.arange(signal.size) / self.sampling_rate if times is None else self._seconds(times)
        active = np.flatnonzero(np.abs(signal) > threshold)
        if active.size == 0:
            return mask
        breaks = np.flatnonzero(np.diff(seconds[active]) > self.max_pause_s) + 1
        for streak in np.split(active, breaks):
            if seconds[streak[-1]] - seconds[streak[0]] >= self.min_continuous_seconds:
                mask[streak[0]:streak[-1] + 1] = True
        return mask

    def is_effective_by_time(self, signal: np.ndarray, threshold: float, times=None) -> bool:
        """Checks if the signal stays active for a continuous minimum duration.

        What is measured is the duration of the active streak in seconds
        (``min_continuous_seconds``), not the number of streaks; see
        :meth:`active_streaks`.

        Args:
            signal (np.ndarray): Input signal array.
            threshold (float): Activity threshold.
            times: Sample timestamps; without them the nominal rate is assumed.

        Returns:
            bool: True if some active streak is long enough. An empty signal is
            never effective.
        """
        return bool(self.active_streaks(signal, threshold, times).any())

    def per_sample_activity_mask(self, signal: np.ndarray, threshold: float) -> np.ndarray:
        """Returns a boolean mask where signal values exceed the threshold.

        Args:
            signal (np.ndarray): Input signal.
            threshold (float): Threshold for activity.

        Returns:
            np.ndarray: Boolean array marking active samples.
        """
        return signal > threshold
    
    def detect_effective_movement(self,activity_windows: pd.DataFrame,
                                  nomf: str = None,vb: int = 0) -> pd.DataFrame:
        """Detects intervals of effective movement from sensor data.

        Args:
            activity_windows (pd.DataFrame): DataFrame containing rows with start_time, 
                                             end_time, codeid_id, and foot.
            nomf (str, optional): Path to an Excel file for exporting raw data 
                                  (default is None).
            vb (int): Verbosity level (0 = silent, 1 = info, 2 = debug).

        Returns:
            pd.DataFrame: Validated segments with effective movement data.
        """

        def segment_fixed_windows(df: pd.DataFrame, window_size: int = 256) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
            """
            Split the data into consecutive fixed-size windows (by sample count).

            Args:
                df (pd.DataFrame): Sorted DataFrame with a '_time' column.
                window_size (int): Number of samples per window.

            Returns:
                List[Tuple[pd.Timestamp, pd.Timestamp]]: (start_time, end_time) for each window.
            """

            if "_time" not in df.columns:
                raise ValueError(i18n._("ERR_MISSING_TIME_COLUMN"))
            
            df = df.sort_values("_time").reset_index(drop=True)

            segments = []
            total_rows = len(df)

            starts = list(range(0, total_rows - window_size + 1, window_size))
            sizes = [window_size] * len(starts)
            covered = len(starts) * window_size
            tail = total_rows - covered
            if tail > 0:
                # The final stretch is not discarded.
                if self.tail_policy == "align" and total_rows >= window_size:
                    starts.append(total_rows - window_size)
                    sizes.append(window_size)
                elif self.tail_policy == "minimum" and tail >= int(self.tail_min_samples):
                    starts.append(covered)
                    sizes.append(tail)
            for i, size in zip(starts, sizes):
                segments.append((df.loc[i, "_time"], df.loc[i + size - 1, "_time"]))

            return segments

        def merge_connected_segments(segments: list[tuple[pd.Timestamp, pd.Timestamp]], 
                                     times: np.ndarray, moving: np.ndarray, max_gap_sec=5.):
            """Merges temporally close segments into a single one.

            Two segments are joined only if the gap between them is at most
            ``max_gap_sec`` and what lies in the gap is known to be movement:
            the gap has data all along (no interval between samples longer
            than ``max_pause_s``) and every sample in it belongs to a sustained
            active streak. A gap without data, or with inactive data, is never
            labelled as movement.

            Args:
                segments (List[Tuple[pd.Timestamp, pd.Timestamp]]): List of segment start and end times.
                times (np.ndarray): Sorted sample timestamps of the series.
                moving (np.ndarray): Per-sample mask of sustained activity.
                max_gap_sec (float): Maximum allowed gap for merging segments.

            Returns:
                List[Tuple[pd.Timestamp, pd.Timestamp]]: Merged list of segments.
            """
            if not segments:
                return []

            def can_merge(previous_end, start) -> bool:
                gap = (start - previous_end).total_seconds()
                if gap <= 0:
                    return True          # overlapping windows (aligned tail)
                if gap > max_gap_sec:
                    return False
                first = int(np.searchsorted(times, np.datetime64(previous_end), side="left"))
                last = int(np.searchsorted(times, np.datetime64(start), side="right"))
                steps = np.diff(times[first:last]) / np.timedelta64(1, "s")
                if len(steps) == 0 or steps.max() > self.max_pause_s:
                    return False         # no data in the gap
                return bool(moving[first + 1:last - 1].all())

            segments = sorted(segments, key=lambda x: x[0])
            merged = []
            current_start, current_end = segments[0]

            for start, end in segments[1:]:
                if can_merge(current_end, start):
                    current_end = max(current_end, end)
                else:
                    merged.append((current_start, current_end))
                    current_start, current_end = start, end

            merged.append((current_start, current_end))
            return merged
        #
        # End of aux fucntion. 
        # Starting point for function detect_effective_movement()

        if "foot" not in activity_windows.columns:
            raise ValueError(i18n._("MVNT-ROOT-MISS"))

        results = []
        writer = ExcelWriter(nomf, engine="xlsxwriter") if nomf else None

        for row in activity_windows.itertuples(index=False):
            segments = []
            valid_segments = []
            
            if vb >= 1:
                print(i18n._("MSG_TMP_IDS").format(id=row.CodeID,ref=row.codeid_id,
                                tstart=row.start_time,tend=row.end_time))
            try:
                start = row.start_time.replace(tzinfo=timezone.utc)
                end = row.end_time.replace(tzinfo=timezone.utc)
            except Exception:
                if self.verbose:
                    print(i18n._("MVNT-TS-NOV").format(row=row))
                continue

            if pd.isnull(start) or pd.isnull(end):
                if self.verbose:
                    print(i18n._("MVNT-TS-NOV").format(row=row))
                continue

            codeid_id = row.codeid_id
            foot = row.foot
            cid = getattr(row, "CodeID", codeid_id)

            if vb > 1:
                print(i18n._("MVNT-QRY-DAT").format(
                    cid=cid, frm=start, dur=(end - start).total_seconds()
                ))

            sensor_data = self.fetch_sensor_data(start, end, codeid_id, foot)
            sensor_data.drop(columns=['result', 'table', '_start', '_stop'],
                             inplace=True, errors='ignore')
            for col in sensor_data.columns:
                if pd.api.types.is_datetime64tz_dtype(sensor_data[col]):
                    sensor_data[col] = sensor_data[col].dt.tz_localize(None)
            if sensor_data.empty:
                continue

            # Export to Excel if requested
            if writer:
                sheet_base = f"{codeid_id}_{foot}_{start.strftime('%H%M%S')}"[:25]
                max_rows = 1_000_000
                for i in range((len(sensor_data) - 1) // max_rows + 1):
                    part = sensor_data.iloc[i*max_rows:(i+1)*max_rows].copy()
                    for col in part.select_dtypes(['datetimetz']).columns:
                        part[col] = pd.to_datetime(part[col], utc=True).dt.tz_localize(None)
                    sheet = f"{sheet_base}_{i+1}"
                    part.to_excel(writer, sheet_name=sheet, index=False)
                    if vb > 0:
                        print(i18n._("MVNT-XLSX-DAT").format(shn=sheet, file=nomf))

            if vb > 1:
                print(i18n._("MVNT-QRY-REC").format(ns=sensor_data.shape[0]))

            sensor_data = self.calculate_magnitude(sensor_data)
            if "|a|" not in sensor_data.columns or "|g|" not in sensor_data.columns:
                continue

            # Asegurar que _time sea datetime y sin zona horaria para la comparación
            sensor_data["_time"] = pd.to_datetime(sensor_data["_time"]).dt.tz_localize(None)
            sensor_data = sensor_data.sort_values("_time").reset_index(drop=True)
            acc = sensor_data["|a|"].to_numpy()
            gyro = sensor_data["|g|"].to_numpy()
            times = sensor_data["_time"].to_numpy()

            # The sampling rate depends on the sock: it is measured, not assumed.
            measured = self.estimate_rate(times)
            if np.isfinite(measured) and abs(measured - self.sampling_rate) / self.sampling_rate > self.fs_tolerance:
                if self.verbose >= 1 or vb >= 1:
                    print(i18n._("MVNT-FS-DEV: {cid} ({foot}): frecuencia medida {fs:.1f} Hz, nominal {nom:g} Hz; "
                                 "el análisis usa la medida").format(cid=cid, foot=foot, fs=measured, nom=self.sampling_rate))

            # Sustained activity is measured on the whole series, in real seconds.
            acc_moving = self.active_streaks(acc - 1, self.accel_threshold, times)
            gyro_moving = self.active_streaks(gyro, self.gyro_threshold, times)

            # We need 256 samples per window for Welch’s method
            segments = segment_fixed_windows(sensor_data, 256)

            for seg_start, seg_end in segments:
                s_start = pd.to_datetime(seg_start).tz_localize(None)
                s_end = pd.to_datetime(seg_end).tz_localize(None)
    
                seg_data = sensor_data[(sensor_data["_time"] >= s_start) & 
                                        (sensor_data["_time"] <= s_end)]
                if vb >= 3:
                    print(f"DEBUG: Ventana {s_start} a {s_end} - " + \
                          f"Filas encontradas: {len(seg_data)}")

                acc_seg = seg_data["|a|"].to_numpy()
                gyro_seg = seg_data["|g|"].to_numpy()

                inside = seg_data.index.to_numpy()
                fs_seg = self.estimate_rate(seg_data["_time"].to_numpy())

                acc_ok = self.is_effective_by_welch(acc_seg, self.accel_power_threshold, fs_seg) and \
                         bool(acc_moving[inside].any())
                gyro_ok = self.is_effective_by_welch(gyro_seg, self.gyro_power_threshold, fs_seg) and \
                          bool(gyro_moving[inside].any())

                if acc_ok or gyro_ok:
                    valid_segments.append((seg_start, seg_end))

            merged_segments = merge_connected_segments(
                valid_segments, times, acc_moving | gyro_moving, max_gap_sec=self.merge_gap_s)
            for mstart, mend in merged_segments:
                results.append({
                    "codeid_id": codeid_id,
                    "start_time": mstart.isoformat(),
                    "end_time": mend.isoformat(),
                    "duration": (mend - mstart).total_seconds(),
                    "leg": foot
                })

            if vb >= 3:
                print(i18n._("LST-SEGS").format(results=pd.DataFrame(results)))
                
            if vb >= 2:
                print(i18n._("MVNT-WLK-FOOT").format(cid=codeid_id, foot=foot,
                    dur=(end - start).total_seconds()))

        if writer:
            writer.close()

        try:
            validated = [EffectiveMovement(**r).model_dump() for r in results]
            return pd.DataFrame(validated)
        except ValidationError as e:
            print(i18n._("MVNT-VAL-EFF-ERR").format(e=e))
            return pd.DataFrame()

    def detect_effective_gait(self, df_effective: pd.DataFrame,vb: int = 0) -> pd.DataFrame:
        """Detects overlapping periods of effective movement for both feet.

        This method determines when both the left and right feet are active
        during the same time interval, suggesting effective gait.

        Args:
            df_effective (pd.DataFrame): DataFrame containing movement segments.
                Required columns: ['codeid_id', 'start_time', 'end_time', 'duration', 'leg']

        Returns:
            pd.DataFrame: DataFrame with overlapping periods (gait episodes).
                Columns: ['codeid_id', 'start_time', 'end_time', 'duration']
        """
        if df_effective.empty:
            return pd.DataFrame(columns=['codeid_id', 'start_time', 'end_time', 'duration'])

        # Coerce the datetime format.
        # Cualquier ISO 8601 es válido, con o sin fracción y con o sin offset.
        df = df_effective.copy()
        for column in ('start_time', 'end_time'):
            try:
                df[column] = pd.to_datetime(df[column], format="ISO8601")
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"{column} debe ser ISO 8601 y todas las filas deben llevar offset o ninguna: {e}"
                ) from None

        gait_rows = []

        for codeid, grp in df.groupby('codeid_id'):
            left = grp[grp['leg'] == 'Left'][['start_time', 'end_time']]
            right = grp[grp['leg'] == 'Right'][['start_time', 'end_time']]
            if left.empty or right.empty:
                continue

            # Cartesian product to find overlapping intervals
            left = left.assign(_key=1)
            right = right.assign(_key=1)
            merged = left.merge(right, on='_key', suffixes=('_L', '_R')).drop(columns='_key')

            for _, r in merged.iterrows():
                st = max(r['start_time_L'], r['start_time_R'])
                en = min(r['end_time_L'], r['end_time_R'])
                if st < en:
                    gait_rows.append({
                        'codeid_id': codeid,
                        'start_time': st,
                        'end_time': en,
                        'duration': (en - st).total_seconds()
                    })

        return pd.DataFrame(gait_rows)

    def save_to_postgresql(self, table_name: str, df: pd.DataFrame, verbose: int = 0) -> None:
        """Saves the given DataFrame to a PostgreSQL table using the DataManager.

        Args:
            table_name (str): Name of the destination table.
            df (pd.DataFrame): DataFrame to store.

        Returns:
            None

        Raises:
            Exception: Any validation or database error raised by the DataManager.
        """
        if df.empty:
            if verbose > 0:
                print(i18n._("PGSQL-INS-TAB-NOD-ERR").format(table_name=table_name))
            return
        self.data_manager.store_data(table_name, df, verbose)
