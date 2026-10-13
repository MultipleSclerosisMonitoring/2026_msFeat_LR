import pandas as pd
from influxdb_client import InfluxDBClient
import psycopg2
from msConfig import influx_client_options, load_config
from msTools.models import CodeID, ActivityLeg, ActivityAll
from msTools import i18n
from msTools.timeutils import ensure_utc, influx_wallclock_z
from msTools.flux import flux_range, flux_string
from msGait.models import EffectiveMovement, EffectiveGait, ActivitySegment
from pydantic import ValidationError
from typing import List, Dict, Optional, Tuple
from psycopg2 import sql
import datetime
import logging


logger = logging.getLogger("msGait.DataManager")

# Modelo de validación de cada tabla en la que se permite escribir.
TABLE_MODELS = {
    "activity_leg": ActivityLeg, "effective_movement": EffectiveMovement, "activity_all": ActivityAll,
    "fullref_sensor_codeid": ActivitySegment, "effective_gait": EffectiveGait, "codeids": CodeID,
}

class DataManager:
    def __init__(self, config_path: str, ids: list = None) -> None:
        """
        Initializes the DataManager with its InfluxDB client.

        The PostgreSQL connection is independent: it is opened the first time
        ``pg_conn`` is used, so InfluxDB-only tasks do not need PostgreSQL.

        :param config_path: Path to the YAML configuration file.
        :type config_path: str
        """
        self._pg_conn = None
        self.influxdb_client = None
        self.config = self.load_config(config_path)
        self.ids = ids

        # Configure InfluxDB client
        self.influxdb_client = InfluxDBClient(**influx_client_options(self.config))
        self.bucket: str = self.config["influxdb"]["bucket"]
        self.measurement: str = self.config['influxdb']['measurement']

    def __del__(self)-> None:
        """Ensure all connections are closed on deletion, even after a partial construction."""
        try:
            self.close_all()
        except Exception:
            pass

    def __enter__(self) -> "DataManager":
        """Allows ``with DataManager(...) as dm:``; connections are closed on exit."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Closes both connections."""
        self.close_all()

    @property
    def pg_conn(self) -> psycopg2.extensions.connection:
        """PostgreSQL connection, opened on first use and reopened if it was closed."""
        if self._pg_conn is None or self._pg_conn.closed:
            self._pg_conn = self._connect_postgresql()
        return self._pg_conn

    def load_config(self, config_path: str) -> Dict:
        """
        Loads configuration from a YAML file.

        :param config_path: Path to the YAML file.
        :type config_path: str
        :return: Configuration dictionary.
        :rtype: dict
        """
        return load_config(config_path)
        
    def get_config(self, sect:str) -> Dict:
        """
        Returns loaded config section kept in sect.

        :param sect: Section interesting inside config.
        :type sect: str
        :return: Dictionary with the section data
        :rtype: dict
        """
        if sect in self.config.keys():
            return self.config[sect]
        else:
            return None

    def _connect_postgresql(self) -> psycopg2.extensions.connection:
        """
        Establishes a connection to PostgreSQL.

        :return: psycopg2 connection object.
        :rtype: psycopg2.extensions.connection
        """
        pg = self.config.get("postgresql")
        if not pg:
            raise ValueError("Falta la sección postgresql en la configuración.")
        options = {}
        # Timeout de conexión opcional (segundos): solo se aplica si está configurado.
        if pg.get("connect_timeout") is not None:
            options["connect_timeout"] = int(pg["connect_timeout"])
        try:
            return psycopg2.connect(
                host=pg["host"],
                database=pg["database"],
                user=pg["user"],
                password=pg["password"],
                port=pg["port"],
                **options,
            )
        except psycopg2.OperationalError as e:
            logger.error(i18n._("PGSQL-CONN-ERR").format(e=e))
            raise

    def close_pg(self) -> None:
        """Closes the PostgreSQL connection if it was opened."""
        connection = getattr(self, "_pg_conn", None)
        if connection is not None and not connection.closed:
            connection.close()
        self._pg_conn = None

    def close_influxdb(self) -> None:
        """Closes the InfluxDB client if it was created."""
        client = getattr(self, "influxdb_client", None)
        if client is not None:
            client.close()

    def close_all(self) -> None:
        """Closes both PostgreSQL and InfluxDB connections."""
        try:
            self.close_pg()
        finally:
            self.close_influxdb()

    def get_influx_client(self) -> InfluxDBClient:
        """
        Returns the InfluxDB client instance.

        :return: InfluxDBClient object.
        :rtype: InfluxDBClient
        """
        return self.influxdb_client

    def check_and_create_tables(self, sql_file_path: str) -> None:
        """
        Checks if required tables exist in PostgreSQL and creates them if missing.

        :param sql_file_path: Path to the SQL file defining the tables.
        :type sql_file_path: str
        """
        try:
            required_tables = [
                "codeids", "effective_movement", "activity_leg", "activity_all"
            ] 

            with self.pg_conn.cursor() as cursor:
                for table_name in required_tables:
                    cursor.execute(f"""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables 
                            WHERE table_name = '{table_name}'
                        );
                    """)
                    exists = cursor.fetchone()[0]
                    if not exists:
                        print(i18n._("INFO_CREATE_TABLE").format(table=table_name, file=sql_file_path))
                        with open(sql_file_path, "r", encoding="utf-8") as sql_file:
                            sql_script = sql_file.read()
                            cursor.execute(sql_script)
                            self.pg_conn.commit()
                    else:
                        print(i18n._("INFO_TABLE_EXISTS").format(table=table_name))
        except Exception as e:
            self.pg_conn.rollback()
            print(i18n._("PGSQL-TAB-ERR").format(e=e))
            raise

    def get_codeids_in_range(self, start_datetime: str, end_datetime: str) -> List[str]:
        """
        Retrieves unique CodeIDs from InfluxDB within a time range.

        :param start_datetime: Start datetime in string format.
        :param end_datetime: End datetime in string format.
        :return: List of unique CodeIDs.
        :rtype: list[str]
        :raises Exception: any query error is logged and raised to the caller.
        """
        try:
            # Influx contains experiment-table wall-clock timestamps serialized as UTC.
            start_iso = influx_wallclock_z(start_datetime)
            end_iso = influx_wallclock_z(end_datetime)

            query = f'''
            from(bucket: {flux_string(self.bucket)})
                |> range({flux_range(start_iso, end_iso)})
                |> filter(fn: (r) => r._measurement == {flux_string(self.measurement)})
                |> keep(columns: ["CodeID"])
                |> distinct()
            '''
            result = self.influxdb_client.query_api().query(
                query, org=self.config['influxdb']['org']
            )
            return [rec['CodeID'] for table in result for rec in table.records]

        except Exception as e:
            # Un fallo de consulta no es lo mismo que un rango sin CodeIDs: se propaga.
            logger.error(i18n._("INFL-QRY-COD-ERR").format(e=e))
            raise
        

    def fetch_data(self, query: str, params: Optional[tuple] = None) -> pd.DataFrame:
        """
        Executes a SQL query in PostgreSQL and returns a DataFrame.
        Supports parameterized queries to prevent SQL injection.

        :param query: SQL query string parameterized.
        :param params: Optional tuple of parameters for the query.
        :return: DataFrame with query results.
        :rtype: pd.DataFrame
        """
        try:
            with self.pg_conn.cursor() as cursor:
                # Ejecución segura pasando los parámetros por separado
                if params:
                    cursor.execute(query, params)
                else:
                    cursor.execute(query)
                    
                columns = [desc[0] for desc in cursor.description]
                data = cursor.fetchall()
                return pd.DataFrame(data, columns=columns)
        except Exception as e:
            # Una lectura fallida deja la transacción abortada: se deshace antes de propagar.
            self._rollback()
            logger.error(i18n._("PGSQL-QRY-GEN-ERR").format(e=e))
            raise

    def _rollback(self) -> None:
        """Rolls back the current transaction if a connection is open."""
        connection = getattr(self, "_pg_conn", None)
        if connection is not None and not connection.closed:
            connection.rollback()

    def segments_retrieval(
        self,
        fstart: Optional[datetime.datetime] = None,
        fend: Optional[datetime.datetime] = None,
        ids: Optional[List[int]] = None,
        verbose: int = 0
    ) -> pd.DataFrame:
        """
        Retrieves records from `activity_all` by IDs or by time window.
        Utilizes parameterized queries for strict security.

        :param fstart: start datetime (if no ids provided)
        :param fend: end datetime (if no ids provided)
        :param ids: list of activity_all IDs
        :param verbose: verbosity level
        :return: DataFrame with columns 
                ['id', 'start_time', 'end_time', 'duration',
                'codeid_ids', 'codeleg_ids', 'active_legs']
        """
        params = None
        
        if ids is not None:
            if verbose >= 1:
                print(i18n._("INFO_SEGMENTS_BY_IDS").format(ids=ids))
            
            # Uso seguro de ANY(%s) en psycopg2 para listas
            query = (
                "SELECT id, start_time, end_time, duration, "
                "codeid_ids, codeleg_ids, active_legs "
                "FROM activity_all WHERE id = ANY(%s) "
                "ORDER BY codeid_ids;"
            )
            params = (ids,)
        else:
            if not fstart or not fend:
                raise ValueError(i18n._("ERR_MISSING_IDS_OR_WINDOW"))
            if verbose >= 1:
                print(i18n._("INFO_SEGMENTS_BY_RANGE").format(start=fstart, end=fend))
            
            # Inserción parametrizada (%s) protegiendo contra SQL Injection
            query = (
                "SELECT id, start_time, end_time, duration, "
                "codeid_ids, codeleg_ids, active_legs "
                "FROM activity_all "
                "WHERE start_time <= %s "
                "  AND end_time   >= %s "
                "ORDER BY codeid_ids;"
            )
            params = (fend, fstart)

        df = self.fetch_data(query, params)
        if df.empty and verbose >= 1:
            print(i18n._("WARN_NO_SEGMENTS_FOUND"))
        else:
            # Due to the lack of TZ info we must change 
            for col in df.columns:
                if pd.api.types.is_datetime64tz_dtype(df[col]):
                    df[col] = df[col].dt.tz_localize(None)
        return df


    def recover_activity_all(self, act: pd.DataFrame, vb: int = 0) -> pd.DataFrame:
        """
        To complete the act DataFrame with the CodeID.

        :param act: Structure with start_time, end_time, Foot, codeid_id.
        :return: DataFrame with the same data plus the CodeID.
        :rtype: pd.DataFrame
        """
        # Cover rows with preparation for 
        activity_leg_like = []
        for _, row in act.iterrows():
            if vb > 1:
                print(i18n._("VB_REG_ACT_ALL").format(row=row))
            for i, foot in enumerate(row["active_legs"]):
                cid = self.fetch_data("SELECT codeid FROM codeids WHERE id = %s;", (int(row["codeid_ids"][i]),))
                activity_leg_like.append({
                    "start_time": row["start_time"], "end_time": row["end_time"],
                    "codeid_id": row["codeid_ids"][i], "CodeID": cid['codeid'][0],
                    "foot": foot})
        df_legs = pd.DataFrame(activity_leg_like)
        if vb > 0:
            print(i18n._("VB-ACT-ALL-LEGS").format(ns=df_legs.shape[0]))
        if df_legs.shape[0] > 0:
            for col in df_legs.columns:
                if pd.api.types.is_datetime64tz_dtype(df_legs[col]):
                    df_legs[col] = df_legs[col].dt.tz_localize(None)
        return df_legs


    def store_codeid(self, codeid: str, verbose: int = 0) -> Tuple[int, bool]:
        """
        Stores a unique CodeID in the `codeids` table and returns its ID.

        :param codeid: the CodeID string to store.
        :return: tuple of (id, is_new_flag).
        :rtype: (int, bool)
        """
        try:
            validated_codeid = CodeID(codeid=codeid)

            with self.pg_conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO codeids (codeid) VALUES (%s) ON CONFLICT (codeid) DO NOTHING RETURNING id;",
                    (validated_codeid.codeid,)
                )
                result = cursor.fetchone()
                if result:
                    new_id = result[0]
                    self.pg_conn.commit()
                    if verbose >= 2:
                        print(i18n._("INFO_CODEID_NEW").format(codeid=codeid, id=new_id))
                    return new_id, True
                
                cursor.execute("SELECT id FROM codeids WHERE codeid = %s;", (validated_codeid.codeid,))
                existing_id = cursor.fetchone()[0]
                if verbose >= 2:
                    print(i18n._("INFO_CODEID_EXIST").format(codeid=codeid, id=existing_id))
                return existing_id, False
        except ValidationError as e:
            print(i18n._("PGSQL-VAL-COD-ERR").format(e=e))
            raise
        except Exception as e:
            self.pg_conn.rollback()
            print(i18n._("PGSQL-INS-COD-ERR").format(e=e))
            raise

    def transform_activityleg(self, data:pd.DataFrame) -> pd.DataFrame:
        """
        Transforms a pandas DataFrame moving time columns to ISO string,
        and CodeID to codeid_id by quering the codeids table.

        The input DataFrame is not modified: the transformation works on a copy.

        :param data:  Activity_Leg pandas.
        :type data: pd.DataFrame
        :return: Updated Activity Leg pandas
        :rtype: pd.DataFrame
        """
        def get_codeid_id_from_db(codeid:str) -> int:
            """
            find the codeid_id by quering the codeids table matching CodeID.

            :param codeid:  CodeID string
            :type data: str
            :return: Id in table codeids
            :rtype: int
            """        
            with self.pg_conn.cursor() as cursor:
                cursor.execute("SELECT id FROM codeids WHERE codeid = %s;", (codeid,))
                return cursor.fetchone()[0] 
        #
        data = data.copy()
        data['start_time'] = data['time_from'].apply(lambda x: x.isoformat())
        data['end_time'] = data['time_until'].apply(lambda x: x.isoformat())
        data['codeid_id'] = data['CodeID'].apply(get_codeid_id_from_db)
        data['duration'] = (data['time_until']-data['time_from']).dt.total_seconds()
        lnom = ['codeid_id','foot','start_time','end_time','duration','mac',\
                'device_name','total_value']
        data.rename(columns={'DeviceName':'device_name','Foot':'foot'}, inplace=True)
        ddat = data.drop(columns=['time_from','time_until','CodeID'])
        return ddat[lnom]
        
    def store_data(self, table_name: str, data: pd.DataFrame, verbose: int = 1) -> List[int]:
        """
        Stores rows into a specified PostgreSQL table after Pydantic validation.

        Every table has a validation model, so only the model's fields can become
        column names, and table and column names are sent as quoted SQL
        identifiers. The input DataFrame is not modified. Either all rows are
        inserted or none: any error rolls the transaction back and is raised to
        the caller.

        :param table_name: destination table name.
        :param data: DataFrame of rows to insert.
        :param verbose: verbosity level.
        :return: list of inserted row IDs (empty if there is nothing to insert).
        :rtype: List[int]
        :raises ValueError: if the table is not one of the known tables.
        :raises pydantic.ValidationError: if a row does not match the table model.
        :raises psycopg2.Error: if the insertion fails.
        """
        model = TABLE_MODELS.get(table_name)
        if model is None:
            raise ValueError(i18n._("ERR_UNKNOWN_TABLE").format(table=table_name))
        if data.empty:
            if verbose > 0:
                print(i18n._("PGSQL-INS-TAB-NOD-ERR").format(table_name=table_name))
            return []

        try:
            if verbose > 0:
                print(i18n._("PGSQL-INS-TAB-INFO"))

            # Convert time columns to strings, on a copy
            data = data.copy()
            for column in ("start_time", "end_time"):
                if column in data.columns:
                    data[column] = data[column].astype(str)

            # Validate rows
            validated_rows = []
            for _, row in data.iterrows():
                row_dict = row.to_dict()
                # Normalize None in codeleg_ids
                if table_name == "activity_all" and "codeleg_ids" in row_dict:
                    row_dict["codeleg_ids"] = [
                        -1 if v is None else int(v) for v in row_dict["codeleg_ids"]
                    ]
                validated_rows.append(model(**row_dict).model_dump())

            # Insert into database
            inserted_ids = [] # List of inserted activity_leg IDs
            with self.pg_conn.cursor() as cursor:
                for row in validated_rows:
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                        sql.Identifier(table_name),
                        sql.SQL(', ').join(map(sql.Identifier, row.keys())),
                        sql.SQL(', ').join(sql.Placeholder() * len(row)),
                    )
                    cursor.execute(statement, tuple(row.values()))
                    inserted_id = cursor.fetchone()[0]  # Obtaining the inserted ID
                    inserted_ids.append(inserted_id)
            self.pg_conn.commit()
            if verbose > 0:
                print(i18n._("PGSQL-INS-TAB-OK").format(table_name=table_name))
            if verbose > 1:
                print(i18n._("PGSQL-LST-INS").format(ids=inserted_ids))
            return inserted_ids
        except ValidationError as e:
            logger.error(i18n._("PGSQL-VAL-TAB-ERR").format(e=e))
            raise
        except Exception as e:
            self._rollback()
            logger.error(i18n._("PGSQL-INS-TAB-ERR").format(e=e, table_name=table_name))
            raise


    def get_real_codeid(self, codeid_id: int) -> str:
        """
        Retrieves the actual CodeID string from its numeric ID.

        :param codeid_id: numeric ID in the codeids table.
        :return: CodeID string.
        :rtype: str
        """
        try:
            query = "SELECT codeid FROM codeids WHERE id = %s;"
            with self.pg_conn.cursor() as cursor:
                cursor.execute(query, (codeid_id,))
                result = cursor.fetchone()
                if result:
                    return result[0]  
                else:
                    raise ValueError(i18n._("ERR_CODEID_NOT_FOUND").format(id=codeid_id))
        except Exception as e:
            print(i18n._("PGSQL-QRY-COD-ERR").format(e=e))
            raise
        return None

    def get_record_all_legs(self, clegs: set, clname : str = "codeleg_ids") -> pd.DataFrame:
        """ 
        Extract the records matching the clname to clegs

        Args:
            clegs (set): pair of legs reference left,right
            colname (str): PostgresQL Column name.

        Returns:
            pd.DataFrame: _description_
        """
        try:
            array_literals = [sql.SQL("ARRAY[{}]").format(
                    sql.SQL(', ').join(map(sql.Literal, pair))
                ) for pair in clegs]

            # Build ARRAY[...] literals for the IN clause
            in_clause = sql.SQL(', ').join(array_literals)
            query = sql.SQL("SELECT * FROM activity_all WHERE {} IN ({})").format(
                sql.Identifier(clname),in_clause)
            with self.pg_conn.cursor() as cursor:
                cursor.execute(query)
                result = cursor.fetchall()
                if result:
                    columns = [desc[0] for desc in cursor.description]
                    df = pd.DataFrame(result, columns=columns)
                    # Due to the lack of TZ info we must change 
                    for col in df.columns:
                        if pd.api.types.is_datetime64tz_dtype(df[col]):
                            df[col] = df[col].dt.tz_localize(None)
                    return df
                else:
                    raise ValueError(i18n._("PGSQL-QRY-CLNAME-NONE").format(clname=clname,clegs=clegs))
        except Exception as e:
            print(i18n._("PGSQL-QRY-COD-ERR").format(e=e))
            raise

    def get_activity_ids_by_start_date_range(
        self, start_datetime: str | datetime.datetime, end_datetime: str | datetime.datetime
    ) -> list[int]:
        """
            Return distinct IDs from ``activity_all`` where ``start_time`` ∈ [start, end].

            Normalizes inputs to UTC and performs a parameterized query on PostgreSQL
            using the existing connection managed by ``DataManager``.

            Args:
                start_datetime: Start of the time window (ISO string or datetime).
                end_datetime: End of the time window (ISO string or datetime).

            Returns:
                A sorted list of unique ``activity_all.id`` values.

            Raises:
                psycopg2.Error: If a database error occurs (rolled back, logged and raised).
        """
        try:
            sdt = ensure_utc(start_datetime)  # → aware UTC datetime
            edt = ensure_utc(end_datetime)

            with self.pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT id
                    FROM activity_all
                    WHERE start_time >= %s AND start_time <= %s
                    ORDER BY id
                    """,
                    (sdt, edt),
                )
                rows = cur.fetchall()
                return [r[0] for r in rows]
        except Exception as e:
            # Un fallo de lectura no es lo mismo que un rango sin actividad: se deshace y se propaga.
            self._rollback()
            logger.error(i18n._("PGSQL-QRY-GEN-ERR").format(e=e))
            raise
