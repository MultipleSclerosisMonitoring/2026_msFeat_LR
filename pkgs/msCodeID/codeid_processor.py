import pandas as pd
from datetime import datetime
from pydantic import ValidationError
from msTools.data_manager import DataManager
from msTools import i18n
from msTools.models import ActivityLeg, ActivityAll
from msGait.models import ActivitySegment
from msTools.timeutils import ensure_utc

class CodeIDProcessor:
    """
    Processes CodeIDs: fetches raw data from InfluxDB,
    identifies activity segments, and prepares them for PostgreSQL.
    """
    def __init__(self, data_manager: DataManager):
        """
        Initialize the CodeID processor.

        :param data_manager: DataManager instance for DB interactions.
        """
        self.data_manager = data_manager
        self.influx_client = data_manager.get_influx_client()
        self.bucket = data_manager.bucket

    def fetch_codeid_data(
        self,
        codeid: str,
        start_datetime: datetime,
        end_datetime: datetime
    ) -> pd.DataFrame:
        """
        Fetch sensor-count data for a given CodeID from InfluxDB.

        :param codeid: Unique CodeID string.
        :param start_datetime: Start of time range.
        :param end_datetime: End of time range.
        :return: DataFrame of InfluxDB query results.
        """
        # Normalize to UTC format
        start_str = ensure_utc(start_datetime).isoformat().replace("+00:00", "Z")
        end_str   = ensure_utc(end_datetime).isoformat().replace("+00:00", "Z")

        query = f'''
        from(bucket: "{self.bucket}")
            |> range(start: {start_str}, stop: {end_str})
            |> filter(fn: (r) => r["CodeID"] == "{codeid}" and r["_field"] == "Ax")
            |> aggregateWindow(every: 1m, fn: count, createEmpty: false)
            |> keep(columns: ["_time", "CodeID", "_field", "_value", "Foot", "lat", "lng", "mac", "DeviceName"])
        '''

        try:
            result = self.influx_client.query_api().query(
                org=self.data_manager.config['influxdb']['org'], query=query
            )
            data = [record.values for table in result for record in table.records]
            df = pd.DataFrame(data).sort_values('_time')
            if df.empty:
                print(i18n._("NO_DATA_CODEID").format(codeid=codeid))
            else:
                print(i18n._("DATA_RETRIEVED_CODEID").format(
                    codeid=codeid, rows=len(df)
                ))
            return df

        except Exception as e:
            # Ensure the exception is cast to string for formatting
            print(i18n._("ERR_INFLUX_FETCH").format(
                codeid=codeid, error=str(e)
            ))
            return pd.DataFrame()

    def identify_activity_segments(
        self,
        df: pd.DataFrame,
        threshold_seconds: float = 70,
        foot: str = 'Left'
    ) -> pd.DataFrame:
        """
        Identify contiguous windows of activity based on time gaps.

        :param df: Raw count DataFrame with a '_time' column.
        :param threshold_seconds: Max gap in seconds to group points together.
        :param foot: 'Left' or 'Right' leg filter.
        :return: DataFrame with columns ['time_from','time_until','CodeID','DeviceName','Foot','total_value','mac'].
        """
        def grouping(block: pd.DataFrame, thresh: float) -> pd.DataFrame:
            """
            Group rows into segments where time gaps or device changes occur.

            :param block: Filtered DataFrame for one leg.
            :param thresh: Threshold in seconds for a new group.
            :return: Aggregated segments with start/end times.
            """
            block = block.assign(
                _time_diff=block['_time'].diff().dt.total_seconds()
            )
            block = block.assign(
                group=((block['_time_diff'] > thresh) |
                       (block['DeviceName'] != block['DeviceName'].shift())).cumsum()
            )
            result = block.groupby('group').agg(
                time_from=pd.NamedAgg(column='_time', aggfunc='first'),
                time_until=pd.NamedAgg(column='_time', aggfunc='last'),
                CodeID=pd.NamedAgg(column='CodeID', aggfunc='first'),
                DeviceName=pd.NamedAgg(column='DeviceName', aggfunc='first'),
                Foot=pd.NamedAgg(column='Foot', aggfunc='first'),
                total_value=pd.NamedAgg(column='_value', aggfunc='sum'),
                mac=pd.NamedAgg(column='mac', aggfunc='first')
            ).reset_index(drop=True)
            return result

        if df.empty:
            print(i18n._("MSG_NO_DATA_DF"))
            # Return empty with expected columns
            return pd.DataFrame(columns=[
                'time_from','time_until','CodeID','DeviceName','Foot','total_value','mac'
            ])

        # Ensure '_time' is datetime with timezone
        if not pd.api.types.is_datetime64_any_dtype(df["_time"]):
            df["_time"] = pd.to_datetime(df["_time"])
            if df["_time"].dt.tz is None:
                df["_time"] = df["_time"].dt.tz_localize("Europe/Madrid")

        # Drop unwanted columns, sort by time
        clean = df.drop(columns=['result','table','_field','lng','lat']) \
                  .sort_values("_time")
        filtered = clean[clean['Foot'] == foot]
        grouped  = grouping(filtered, threshold_seconds)

        return grouped if not grouped.empty else pd.DataFrame(columns=[
            'time_from','time_until','CodeID','DeviceName','Foot','total_value','mac'
        ])

    def inter_segs(
        self,
        sg1: pd.DataFrame,
        sg2: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Compute intersections between two sets of time segments.

        :param sg1: DataFrame of segments for leg 1.
        :param sg2: DataFrame of segments for leg 2.
        :return: DataFrame of overlapping intervals with index refs.
        """
        def overlaps(r):
            return (r['time_from_1'] <= r['time_until_2'] and
                    r['time_from_2'] <= r['time_until_1'])

        def intersection(r):
            return pd.Series({
                'time_from': max(r['time_from_1'], r['time_from_2']),
                'time_until': min(r['time_until_1'], r['time_until_2'])
            })

        if sg1.empty or sg2.empty:
            return pd.DataFrame(columns=[
                'time_from','time_until','R1_id','R2_id','codeid_id_1','codeid_id_2'
            ])

        a = sg1.reset_index().rename(columns={'index':'R1_id'})
        b = sg2.reset_index().rename(columns={'index':'R2_id'})
        cross = a.merge(b, how='cross', suffixes=('_1','_2'))
        cross['intersects'] = cross.apply(overlaps, axis=1)
        intr = cross[cross['intersects']]
        if intr.empty:
            return pd.DataFrame(columns=[
                'time_from','time_until','R1_id','R2_id','codeid_id_1','codeid_id_2'
            ])

        intr.loc[:, ['time_from','time_until']] = intr.apply(intersection, axis=1)
        return intr[['time_from','time_until','R1_id','R2_id','codeid_id_1','codeid_id_2']].reset_index(drop=True)

    def merge_activity_legs_to_all(
        self,
        act_segR: pd.DataFrame,
        act_segL: pd.DataFrame,
        inter: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Merge left and right leg segments into a single DataFrame
        ready for insertion into the 'activity_all' table.
        """
        def format_mac(addr: str) -> str:
            """Convert hyphenated MAC suffix into colon-separated hex."""
            raw = addr.split('-')[-1]
            return ':'.join(raw[i:i+2] for i in range(0, len(raw), 2))

        # Join on R1_id and R2_id, then select relevant columns
        merged = inter.merge(
            act_segR[['CodeID','device_name','foot','mac','codeleg_id']],
            left_on='R1_id', right_index=True, suffixes=('','_R')
        ).merge(
            act_segL[['CodeID','device_name','foot','mac','codeleg_id']],
            left_on='R2_id', right_index=True, suffixes=('_R','_L')
        )

        cols = [
            'time_from','time_until',
            'CodeID_R','device_name_R','foot_R','mac_R',
            'CodeID_L','device_name_L','foot_L','mac_L',
            'codeid_id_1','codeid_id_2','codeleg_id_R','codeleg_id_L'
        ]
        df = merged[cols].copy()

        # Ensure MACs are colon-separated
        if not df['mac_R'].str.contains(':').any():
            df['mac_R'] = df['mac_R'].apply(format_mac)
        if not df['mac_L'].str.contains(':').any():
            df['mac_L'] = df['mac_L'].apply(format_mac)

        # Build final columns and drop intermediates
        df['is_effective'] = False
        df['duration'] = (df['time_until'] - df['time_from']).dt.total_seconds()
        df['macs'] = df.apply(lambda r: [r['mac_L'], r['mac_R']], axis=1)
        df['codeid_ids'] = df.apply(lambda r: [r['codeid_id_2'], r['codeid_id_1']], axis=1)
        df['codeleg_ids'] = df.apply(lambda r: [r['codeleg_id_L'], r['codeleg_id_R']], axis=1)
        df['device_names'] = df.apply(lambda r: [r['device_name_L'], r['device_name_R']], axis=1)
        df['active_legs'] = df.apply(lambda r: [r['foot_L'], r['foot_R']], axis=1)

        df.rename(columns={'time_from':'start_time','time_until':'end_time'}, inplace=True)
        df.drop(columns=[
            'CodeID_R','device_name_R','foot_R','mac_R',
            'CodeID_L','device_name_L','foot_L','mac_L',
            'codeleg_id_L','codeleg_id_R'
        ], inplace=True)

        return df

    def save_to_postgresql(self, table_name: str, df: pd.DataFrame) -> None:
        """
        Save processed DataFrame to a PostgreSQL table using DataManager.

        :param table_name: Destination table name.
        :param df: DataFrame to insert.
        """
        if df.empty:
            print(i18n._("MSG_NO_DATA_SAVE").format(table=table_name))
            return

        try:
            self.data_manager.store_data(table_name, df)
        except Exception as e:
            print(i18n._("ERR_SAVE_TABLE").format(
                table=table_name, error=str(e)
            ))
