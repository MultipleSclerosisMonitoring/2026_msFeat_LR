import pandas as pd
import numpy as np

from datetime import timezone
from pandas import ExcelWriter
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

from msTools.data_manager import DataManager
from msTools import i18n
from msTools.timeutils import ensure_utc
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
                if verbose >= 1:
                    if ids_from_time:
                        print(f"[MovementDetector] {len(ids_from_time)} ID(s) found in activity_all for the time window.")
                        self.ids = ids_from_time
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
        params = self.data_manager.get_config(sect)
        self.freq_band = (params["freq_band_min"], params["freq_band_max"])
        self.min_continuous_hits = params["min_continuous_hits"]
        self.accel_threshold = params.get("accel_threshold", 0.2)
        self.gyro_threshold = params.get("gyro_threshold", 50)
        self.accel_power_threshold = params.get("accel_power_threshold",0.1)
        self.gyro_power_threshold = params.get("gyro_power_threshold",1000)

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

        start_time = ensure_utc(start_time).isoformat().replace("+00:00", "Z")
        end_time = ensure_utc(end_time).isoformat().replace("+00:00", "Z")

        query = f'''
        from(bucket: "{self.data_manager.bucket}")
            |> range(start: {start_time}, stop: {end_time})
            |> filter(fn: (r) => r["CodeID"] == "{codeid}" and r["Foot"] == "{foot}")
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

    def is_effective_by_welch(self, signal: np.ndarray, power_threshold) -> bool:
        """Determines if a signal contains sufficient power in a frequency band.

        Args:
            signal (np.ndarray): Input signal array.

        Returns:
            bool: True if power within the target frequency band exceeds threshold.
        """
        freqs, power = welch(signal, fs=self.sampling_rate)
        band_power = power[(freqs >= self.freq_band[0]) & (freqs <= self.freq_band[1])].sum()
        return band_power >= power_threshold

    def is_effective_by_time(self, signal: np.ndarray, threshold: float) -> bool:
        """Checks if the signal remains active for a continuous minimum duration.

        Args:
            signal (np.ndarray): Input signal array.
            threshold (float): Activity threshold.

        Returns:
            bool: True if signal is continuously active long enough.
        """
        std_signal = np.std(signal)
        if std_signal < 0.01:
            return False
        active = np.abs(signal) > threshold
        consecutive = np.diff(
            np.where(
                np.concatenate(([active[0]], active[:-1] != active[1:], [True]))
            )[0]
        )[::2]
        return (len(consecutive) >= self.min_continuous_hits)

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

            for i in range(0, total_rows - window_size + 1, window_size):
                start_time = df.loc[i, "_time"]
                end_time = df.loc[i + window_size - 1, "_time"]
                segments.append((start_time, end_time))

            return segments

        def merge_connected_segments(segments: list[tuple[pd.Timestamp, pd.Timestamp]], 
                                     max_gap_sec=5.):
            """Merges temporally close segments into a single one.

            Args:
                segments (List[Tuple[pd.Timestamp, pd.Timestamp]]): List of segment start and end times.
                max_gap_sec (float): Maximum allowed gap for merging segments.

            Returns:
                List[Tuple[pd.Timestamp, pd.Timestamp]]: Merged list of segments.
            """
            if not segments:
                return []

            segments = sorted(segments, key=lambda x: x[0])
            merged = []
            current_start, current_end = segments[0]

            for start, end in segments[1:]:
                gap = (start - current_end).total_seconds()
                if gap <= max_gap_sec:
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
                start = row.start_time.replace(tzinfo=timezone.utc) # ensure_utc(row.start_time)
                end = row.end_time.replace(tzinfo=timezone.utc)    # ensure_utc(row.end_time)
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

            sensor_data = sensor_data.sort_values("_time").reset_index(drop=True)
            acc = sensor_data["|a|"].to_numpy()
            gyro = sensor_data["|g|"].to_numpy()

            # We need 256 samples per window for Welch’s method
            segments = segment_fixed_windows(sensor_data, 256)
            # Asegurar que _time sea datetime y sin zona horaria para la comparación
            sensor_data["_time"] = pd.to_datetime(sensor_data["_time"]).dt.tz_localize(None)
            sensor_data = sensor_data.sort_values("_time").reset_index(drop=True)
            
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

                acc_ok = self.is_effective_by_welch(acc_seg,self.accel_power_threshold) and \
                         self.is_effective_by_time(acc_seg-1, self.accel_threshold)
                gyro_ok = self.is_effective_by_welch(gyro_seg, self.gyro_power_threshold) and \
                          self.is_effective_by_time(gyro_seg, self.gyro_threshold)

                if acc_ok or gyro_ok:
                    valid_segments.append((seg_start, seg_end))

            merged_segments = merge_connected_segments(valid_segments, max_gap_sec=10)
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
        df = df_effective.copy()
        df['start_time'] = df['start_time'].apply(lambda x: x if '.' in x else x + '.000000')
        df['start_time'] = pd.to_datetime(df['start_time'], format="%Y-%m-%dT%H:%M:%S.%f")
        df['end_time'] = df['end_time'].apply(lambda x: x if '.' in x else x + '.000000')
        df['end_time'] = pd.to_datetime(df['end_time'], format="%Y-%m-%dT%H:%M:%S.%f")

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
        """
        if df.empty and verbose > 0:
            print(i18n._("PGSQL-INS-TAB-NOD-ERR").format(table_name=table_name))
            return
        try:
            self.data_manager.store_data(table_name, df, verbose)
        except Exception as e:
            print(i18n._("PGSQL-INS-TAB-ERR").format(fable_name=table_name, e=e))

