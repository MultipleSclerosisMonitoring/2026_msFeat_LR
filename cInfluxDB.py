import pandas as pd
from influxdb_client import InfluxDBClient
from datetime import datetime
import urllib3
import yaml

# Desactiva las advertencias de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class cInfluxDB:
    def __init__(self, config_path: str, timeout: int = 500_000):
        """
        Initializes the connection to InfluxDB using a YAML configuration file.

        :param config_path: Path to the YAML configuration file.
        :type config_path: str
        :param timeout: Connection timeout in milliseconds.
        :type timeout: int

        """
        # Load the configuration from the YAML file
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)

        # Extract the necessary values
        self.bucket = config['influxdb']['bucket']
        self.org = config['influxdb']['org']
        self.token = config['influxdb']['token']
        self.url = config['influxdb']['url']

        # Initialises the InfluxDB client
        self.client = InfluxDBClient(url=self.url, token=self.token, org=self.org, \
                                     verify_ssl=False, timeout=timeout)
        self.measurement = self.bucket.split("/")[0] if '/' in self.bucket else \
                           self.bucket
    

    def query_data(self, from_date: datetime, to_date: datetime, qtok: str, pie: str, 
                   metrics=None) -> pd.DataFrame:
        """
        Query data in InfluxDB, pivoting the results to get the metrics in columns.

        :param from_date: Start date (ISO 8601 format: 'YYYYY'-MM-DDTHH:MM:SSZ).
        :type from_date: datetime
        :param to_date: End date (ISO 8601 format: 'YYYYY'-MM-DDTHH:MM:SSZ).
        :type to_date: datetime
        :param qtok: CodeID 
        :type qtok: str
        :param pie: Left or Right foot ('Right', 'Left')
        :type pie: str
        :param metrics: List of metrics to query (default: predefined set)
        :type metrics: list[str], optional

        :return: DataFrame with the metrics pivoted on columns, ordered by _time descending.
        :rtype: pd.DataFrame
        """
        from_date_str = from_date.strftime('%Y-%m-%dT%H:%M:%SZ')  # UTC con 'Z'
        to_date_str = to_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Default metrics
        if metrics is None:
            metrics = ['Ax', 'Ay', 'Az', 'Gx', 'Gy', 'Gz', 'Mx', 'My', 'Mz', 'S0', 'S1', 'S2']
        
        metrics_str = ' or '.join([f'r._field == "{metric}"' for metric in metrics])
        columns_str = ', '.join([f'"{metric}"' for metric in metrics])


        query = f'''
        from(bucket: "{self.bucket}")
            |> range(start: {from_date_str}, stop: {to_date_str})
            |> filter(fn: (r) => r._measurement == "{self.measurement}")
            |> filter(fn: (r) => {metrics_str} or r._field == "lat" or r._field == "lng")
            |> filter(fn: (r) => r["CodeID"] == "{qtok}" and r["type"] == "SCKS" and r["Foot"] == "{pie}")
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> keep(columns: ["_time", {columns_str}, "lat", "lng"])
        '''

        try:
            result = self.client.query_api().query(org=self.org, query=query)
        except Exception as e:
            print(f"Error in the query: {str(e)}")
            raise

        # Process the results in a DataFrame
        data = []
        for table in result:
            for record in table.records:
                val = record.values
                # val["Latitude"] = val.get("lat", None)
                # val["Longitude"] = val.get("lng", None)
                data.append(val)

        df = pd.DataFrame(data)

        # Elimina columnas internas que pueden molestar
        df = df.drop(columns=[col for col in ['result', 'table'] if col in df.columns])

        return df.sort_values(by="_time", ascending=False).reset_index(drop=True)

   

    def query_with_aggregate_window(self, from_date: datetime, to_date: datetime, window_size: str = "20ms", qtok: str = None, pie: str = None, metrics=None) -> pd.DataFrame:
        """
        Query data in InfluxDB with aggregateWindow, pivoting the results to get metrics as columns.

        :param from_date: Start datetime (ISO 8601 format: 'YYYY-MM-DDTHH:MM:SSZ').
        :type from_date: datetime
        :param to_date: End datetime (ISO 8601 format: 'YYYY-MM-DDTHH:MM:SSZ').
        :type to_date: datetime
        :param window_size: Aggregation window size (default: '20ms').
        :type window_size: str
        :param qtok: CodeID (required).
        :type qtok: str
        :param pie: Left or Right foot ('Right', 'Left') (required).
        :type pie: str
        :param metrics: List of metrics to query (default: predefined set).
        :type metrics: list[str], optional
        :return: DataFrame with metrics as columns, ordered by _time.
        :rtype: pd.DataFrame
        """

        if not qtok or not pie:
            raise ValueError("Los argumentos 'qtok' y 'pie' son obligatorios para esta consulta.")

        from_date_str = from_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        to_date_str = to_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Default metrics
        if metrics is None:
            metrics = ['Ax', 'Ay', 'Az', 'Gx', 'Gy', 'Gz', 'Mx', 'My', 'Mz', 'S0', 'S1', 'S2']

        metrics_str = ' or '.join([f'r._field == "{metric}"' for metric in metrics])
        columns_str = ', '.join([f'"{metric}"' for metric in metrics + ["Latitude", "Longitude"]])

        query = f'''
            from(bucket: "{self.bucket}")
            |> range(start: time(v: "{from_date_str}"), stop: time(v: "{to_date_str}"))
            |> filter(fn: (r) => r._measurement == "{self.measurement}")
            |> filter(fn: (r) => {metrics_str} or r._field == "Latitude" or r._field == "Longitude")
            |> filter(fn: (r) => r["CodeID"] == "{qtok}" and r["type"] == "SCKS" and r["Foot"] == "{pie}")
            |> group(columns: ["_field"])
            |> aggregateWindow(every: {window_size}, fn: last, createEmpty: false)
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
            |> keep(columns: ["_time", {columns_str}, "lat", "lng"])

        '''
        

        try:
            result = self.client.query_api().query(org=self.org, query=query)
        except Exception as e:
            print(f"Error en la consulta: {str(e)}")
            raise

        # Process the results in a DataFrame
        data = []
        for table in result:
            for record in table.records:
                data.append(record.values)

        df = pd.DataFrame(data).drop(['result', 'table'], axis=1)

        # Make sure that all the metrics are in the DataFrame
        for col in ["_time"] + metrics:
            if col not in df:
                df[col] = None  

        return df.sort_values(by="_time", ascending=False).reset_index(drop=True)


    def debug_fields(self):
        """
        Prints all available field names (_field) in the bucket to check what can be queried.
        """
        query = f'''
        import "influxdata/influxdb/schema"
        schema.fieldKeys(bucket: "{self.bucket}")
        '''
        try:
            result = self.client.query_api().query(org=self.org, query=query)
            print("Available fields (_field) in the bucket:")
            for table in result:
                for record in table.records:
                    print(f"- {record.get_value()}")
        except Exception as e:
            print(f"Error al listar los campos: {e}")
    
    def show_raw_sample(self, from_date, to_date, qtok, pie):
        query = f'''
        from(bucket: "{self.bucket}")
        |> range(start: {from_date.strftime('%Y-%m-%dT%H:%M:%SZ')}, stop: {to_date.strftime('%Y-%m-%dT%H:%M:%SZ')})
        |> filter(fn: (r) => r._measurement == "{self.measurement}")
        |> filter(fn: (r) => r["CodeID"] == "{qtok}" and r["Foot"] == "{pie}" and r["type"] == "SCKS")
        |> limit(n: 5)
        '''
        try:
            result = self.client.query_api().query(org=self.org, query=query)
            for table in result:
                for record in table.records:
                    print(record.values)
        except Exception as e:
            print(f"Error al ejecutar la consulta: {e}")
