# Configuración única: YAML + .env

El cargador `msConfig.load_config` es compartido por el extractor, el pipeline directo, `cInfluxDB` y `DataManager` (incluidos los comandos y la API de `pkgs`). Solo lee configuración; no abre conexiones ni modifica `os.environ`.

## Preparación

Para una instalación nueva desde la raíz:

```bash
pipx install poetry                 # una sola vez en el equipo
poetry install --all-extras         # crea .venv/ con las versiones de poetry.lock
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

En PowerShell, activar con `.\.venv\Scripts\Activate.ps1` y copiar con `Copy-Item`. Completar `.env` con los datos de conexión. La migración realizada en este espacio de trabajo ya creó `config/config.yaml` y `.env` a partir de `pkgs/.config.yaml`; no volver a copiar las plantillas sobre esos archivos.

`.env` contiene URL, organización, bucket y token InfluxDB, y host, usuario, contraseña y base PostgreSQL. Es local, ignorado por Git y creado con permisos 0600 en esta migración. Los YAML de compatibilidad ahora usan `${INFLUXDB_TOKEN}`, `${POSTGRES_PASSWORD}`, etc.; ningún cargador conserva una segunda implementación de lectura YAML.

Se conserva el archivo privado ignorado `InfluxDBms/.config_db.yaml`, ahora también con referencias a variables. Los YAML ya seguidos por Git se sustituyen por referencias, no se eliminan. Esto retira los valores del árbol actual, pero no del historial Git: si eran secretos válidos publicados, su rotación y limpieza histórica son acciones independientes.

## Parámetros y rutas

`config/config.example.yaml` documenta el esquema completo y conserva los parámetros de movimiento de la configuración de referencia:

- `influxdb`: conexión, `measurement`, `timeout` en milisegundos, `verify_ssl` y `ssl_ca_cert`. El certificado TLS se verifica por defecto; `ssl_ca_cert` es opcional, para una CA propia. Si se desactiva con `verify_ssl: false` (o el nombre antiguo `verify`), cada ejecución lo avisa en nivel 0 y los avisos de urllib3 no se silencian. La URL debe usar el nombre del servidor que figura en su certificado, no la IP.
- `postgresql`: conexión y `port`; el puerto ya se transmite a psycopg2. Esta sección se puede eliminar en configuraciones que solo usan InfluxDB. Si existe, sus variables también deben estar completas.
- `paths.registry_csv`, `paths.output_folder`, `paths.h5_path`, `paths.output_dataset`: entrada y salidas del pipeline. Si falta alguna, el cargador aplica un único valor por defecto relativo a la raíz del repositorio (`tests.csv`, `data/raw`, `data/raw/gait_study_data.h5`, `DatasetMaestro.xlsx`), nunca al directorio de ejecución.
- `processing.fs`: frecuencia usada por el pipeline directo en cadencia y decaimiento. Debe ser finita y mayor que 10 Hz por los filtros actuales. No corrige por sí misma el muestreo irregular ni las limitaciones de los algoritmos descritas en la revisión.
- `batch.test_type`: selección para extracción HDF5; `project.language`: idioma del pipeline directo.
- `movement`: parámetros heredados de detección. Su motor mantiene su contrato de frecuencia recibido por el constructor; no utiliza `processing.fs` automáticamente.

Todas las entradas de `paths` se resuelven respecto al directorio del YAML. Por eso la plantilla en `config/` usa `../tests.csv` y `../data/raw`. Son independientes del directorio de ejecución. Las rutas pasadas como argumentos CLI se interpretan respecto al directorio de ejecución.

## Selección y precedencia

1. Se usa el YAML indicado con `--config`; sin argumento, `config/config.yaml` y, si falta, `pkgs/.config.yaml` como compatibilidad.
2. Una ruta explícita se busca primero respecto al directorio de ejecución, después respecto a la raíz. Se siguen aceptando `config_db.yaml` y `.config_db.yaml` como nombres en `InfluxDBms/`.
3. Configuraciones del repositorio usan el `.env` de la raíz, sin búsqueda en directorios padre. Un YAML externo usa el `.env` de su propio directorio. La API Python acepta `dotenv_path=...` para selección explícita.
4. Variables del proceso tienen prioridad sobre `.env`. El YAML solicita variables mediante `${NOMBRE}`; una variable definida en el proceso no sustituye un literal YAML sin marcador. Se expanden mapas/listas y marcadores dentro de cadenas.
5. No se hace interpolación dentro de `.env`: se conservan literalmente `$`, `#` y otros caracteres de los secretos correctamente entrecomillados. No se expanden variables en cascada.
6. CLI tiene prioridad sobre las rutas/tipo de test YAML que expone cada comando. Después se usan valores por defecto documentados: timeout 30000 ms, measurement Gait, puerto 5432, fs 100 en el pipeline directo, test 6MWT e idioma es.

Se rechazan variables ausentes/vacías, YAML no estructurado, conexiones incompletas, URL HTTP(S) inválida, booleanos distintos de true/false, timeout/puerto inválidos y frecuencia incompatible. Los errores no incluyen los valores secretos. No imprimir el diccionario resuelto: contiene credenciales.

## Ejecución

`msfeat-pipeline` es el único comando: extrae de InfluxDB, guarda las señales, calcula las métricas y publica el Excel. Se ejecuta en el entorno de Poetry (`poetry run msfeat-pipeline ...`, o tras `poetry shell`); `python pipeline_test.py` es un lanzador de la misma orden.

Comprobar configuración sin conectar:

```bash
msfeat-pipeline --check-config
msfeat-pipeline --check-config --config pkgs/.config.yaml
```

Procesar el registro configurado:

```bash
msfeat-pipeline
msfeat-pipeline --config config/config.yaml --csv tests.csv --test-type 6MWT
```

`--check-config` solo verifica configuración, no autentica ni demuestra conectividad. `consulta_influxdb.py` (exploración manual, código heredado) y `msfeat-graficas` (figuras a partir del Excel) son auxiliares y no forman parte del pipeline.

Los comandos de `pkgs` siguen aceptando `-c config/config.yaml`. Para usarlos se requiere también su entorno de dependencias (`python -m pip install ./pkgs`); el proyecto raíz no instala todos los subsistemas heredados de PostgreSQL/API.

## Selección, reanudación y niveles de información

- `--ids 58,60`: procesa solo esos `id` del registro.
- `--test-type 6MWT`: procesa solo ese tipo de test; sin la opción, todos.
- `--missing-only`: usa el manifest para no volver a consultar los eventos completados; sus métricas se recalculan leyendo el almacén.
- `--workers N`: N consultas simultáneas a InfluxDB (por defecto 1). La escritura y el análisis siguen en un único hilo y en el orden del registro.
- Reintentos: si una consulta falla por conexión se reintenta tras las esperas de `influxdb.retry_waits_s` (5, 15 y 45 s); el número de intentos queda en el manifest y en `Estado_Eventos`.
- `--stored-only`: recalcula solo los eventos ya guardados, sin ninguna consulta a InfluxDB. Es el modo para probar cambios de algoritmo o de parámetros.
- `--audit`: compara el almacén con el registro sin consultar InfluxDB.
- `--verbose 0|1|2`: 0 muestra avisos y errores; 1 (por defecto) añade el progreso por evento y el resumen final; 2 añade el detalle de depuración (coste por evento, verificaciones, eventos reutilizados, ensayos repetidos).

El registro se valida entero antes de conectar. Las filas inválidas seleccionadas se devuelven con estado `INVALID_ROW`; las que quedan fuera de la selección solo se informan (nivel 1).

### Almacén de señales

`paths.h5_path` da nombre al almacén: con `data/raw/gait_study_data.h5`, las señales se guardan en el directorio `data/raw/gait_study_data/`, con un archivo por evento:

```text
data/raw/gait_study_data/p_{codeid}/{t_code}/trial_{id}.h5   # nodos Left, Right (o Unknown)
data/raw/gait_study_data.manifest.jsonl
data/raw/gait_study_data.h5.lock                              # solo mientras se escribe
```

Cada evento se escribe completo en un temporal, se relee (filas, instantes, columnas y atributos) y solo entonces sustituye al archivo definitivo. Un corte del proceso no puede dañar eventos ya publicados. Cada nodo guarda como atributos `id`, fechas, paciente, test, pie y dispositivo. La clave lógica que aparece en el manifest es `/p_{codeid}/{t_code}/trial_{id}/{pie}`. Un archivo único `gait_study_data.h5` del formato anterior no se usa: se avisa y se ignora.

El manifest tiene una línea `run_start`, una línea `event` por evento (estado, filas por pie, dispositivo, duplicados eliminados, tiempo y memoria) y una línea `run_end`. Una ejecución sin `run_end` quedó interrumpida. Estados: `OK`, `EMPTY`, `INVALID_ROW`, `QUERY_ERROR`, `WRITE_ERROR`, `SKIPPED`.

Solo puede haber un proceso escritor. El bloqueo guarda el identificador del proceso: si ese proceso ya no existe (ejecución cortada), la siguiente ejecución retira el bloqueo y los temporales con un aviso y continúa; `--missing-only` reanuda desde el último evento publicado.

### Reglas de normalización y agregación

Por evento y pie: si hay más de un dispositivo se conserva el que tiene más muestras; las filas idénticas se eliminan; de las que repiten `_time` con valores distintos se conserva la primera. Todo ello se avisa y queda en el manifest. InfluxDB no tiene etiqueta de sesión (etiquetas: `CodeID`, `DeviceName`, `Foot`, `app`, `lat`, `lng`, `mac`, `type`), así que la serie física se identifica por evento, pie y dispositivo.

Por paciente: ningún ensayo se descarta. La asimetría bilateral se calcula solo dentro de cada evento; después, cada métrica del paciente es la media de todos sus ensayos válidos de ese test, y la asimetría la media de sus ensayos bilaterales.

El Excel se escribe en un temporal, se relee y solo entonces sustituye al anterior. Es el único informe del proyecto y su primera hoja, `Leeme`, declara el alcance y el contenido de cada hoja: `Datos_Pacientes` (una fila por paciente), `Agregacion_Pacientes`, `Ensayos` (una fila por evento y pie, con estado, motivo, cobertura y filtro de cada métrica), `Asimetria` (con `bilateral_available`), `Balanceos`, `Estado_Eventos` (con el número de métricas con valor de cada evento), `Parametros`, `Procedencia` (commit de git, huellas de la configuración y del registro, fecha y recuentos), `Estadisticas_Ensayos` y `Estadisticas_Globales`. Las estadísticas usan una lista explícita de métricas e informan N total, N con valor y N sin valor además de media, mediana y desviación típica, entre ensayos y entre pacientes.

### Procesado de señales

Los parámetros están en la sección `processing` del YAML (plantilla comentada en `config/config.example.yaml`). Se validan al arrancar (tipos, signos, cortes por debajo de Nyquist, límites que no se contradigan) y los valores usados se exportan en la hoja `Parametros`.

- **Tiempo real.** Cada serie exige `_time`. Las marcas de `_time` son de recepción en el móvil y llegan en ráfagas, mientras que el sensor muestrea a ritmo constante. Por eso la frecuencia efectiva se mide como muestras por segundo de señal continua (no depende de las ráfagas, que se informan como `irregularidad_tiempo`) y, con `time_grid: uniform`, las muestras de cada tramo continuo se recolocan a paso constante: no se interpola ni se añaden o quitan filas, solo se corrige el instante de cada muestra. `processing.fs` (50 Hz) es la nominal; como la real depende del sensor, las series distintas se señalan en `fs_desviada` y se resumen en nivel 1 (`INFO_FS_RATES`).
- **Recepción retrasada.** Antes de buscar huecos, en cada intervalo de más de 100 ms entre filas se cuentan las muestras recibidas en el intervalo más los 200 ms siguientes y se comparan con las que corresponden por duración. Si llega el 90 % o más, el móvil recibió tarde un bloque completo: no hay hueco y las marcas se reparten de forma uniforme (`intervalos_recuperados`). Si llega menos, es un hueco real y las zancadas que lo atraviesan se excluyen.
- **Huecos, por grupo de sensores.** Hay dos grupos: IMU (`Ax` a `Gz`) y presión (`S0` a `S2`). Una fila aporta un valor nuevo a un grupo si cambia alguno de sus canales. Si pasan más de `max_gap_s` (100 ms) sin valor nuevo hay un hueco y la serie se corta para ese grupo; hasta ese umbral las filas sin valor nuevo se interpolan. Nada se sustituye por cero. En la presión la regla se aplica siempre y se exige estar fuera de huecos al menos `min_coverage` (80 %) del tiempo.
- **Calidad de la IMU.** Con el pie quieto, la IMU repite el mismo valor y eso es una medida válida, no un hueco: la regla de los 100 ms se aplica solo con el pie en movimiento (giroscopio por encima de 100 °/s en las muestras vecinas, o la presión cambiando de estado). La IMU de una serie es válida si, mientras el pie se mueve, repite valor en menos del 10 % de las filas (`imu_repeticion_en_movimiento_prc`, `imu_valida`); si no, no se informan longitud, altura ni giros.
- **Qué cubre la hipótesis de continuidad.** No hay contador de muestra ni de paquete en los datos. Dentro de un tramo sin huecos se supone que no falta ninguna muestra. Eso cubre las ráfagas, los retrasos de recepción y los huecos que dejan menos del 90 % de las muestras esperadas. No cubre las pérdidas pequeñas que no dejan un intervalo de más de 100 ms ni bajan del 90 %: en ese caso el tiempo del tramo queda ligeramente comprimido.
- **Vertical estimada.** El calcetín no siempre queda igual de orientado. En cada serie se estima la dirección de la gravedad con las muestras en reposo y se proyectan sobre ella la aceleración (`Av`, vertical) y el giroscopio (`Wv`, giro en torno a la vertical). `inclinacion_grados` es el ángulo entre esa vertical y el eje `Ay`; si no hay reposo utilizable se usan `Ay` y `Gy` y `vertical_estimada` es falso.
- **Zancadas por contacto de talón.** `S2` es el talón; `S0` y `S1`, el antepié. Un contacto de talón es el paso de descargado a cargado de `S2`. Dos contactos no pueden estar a menos de `cycle_min_s` (0,8 s) y una zancada vale si dura entre 0,8 y 2,5 s. La calidad de la presión se comprueba antes de detectar; si no es suficiente no se cuenta nada, y no se cambia a otra señal. La cadencia es 60 entre el tiempo medio de zancada, y solo se informa con el mínimo de zancadas válidas de la prueba.
- **Mínimo de zancadas por prueba.** 6MWT: 30 en el ensayo completo y 20 en cada minuto que compara el decaimiento. T25FW: 4, juntando las zancadas de los intentos del mismo paciente y día; el resultado es orientativo y aparece en `Datos_Pacientes` (cada intento por separado queda sin cadencia). TUG: no se informan cadencia, fases, longitud, altura ni asimetría; su métrica es la duración. 2MWT: 10 (provisional). El mínimo aplicado está en la columna `min_zancadas`.
- **Giros por ángulo acumulado.** La velocidad de giro en torno a la vertical se suaviza para quitar la oscilación de cada zancada. Un episodio tiene un núcleo por encima de `turn_threshold` y se extiende a ambos lados hasta bajar de `turn_extend_threshold`, para no perder el ángulo del arranque y del final. Es un giro si el ángulo acumulado llega a `turn_min_angle_deg` (90°). Retirar los giros no pega tramos separados.
- **Fases.** Cada ciclo es una zancada válida. El pie está en el suelo si algún sensor de presión está cargado; el resto del ciclo es balanceo. Un ciclo solo cuenta si su balanceo dura 0,25-0,8 s y ocupa el 20-55 % del ciclo.
- **Longitud de zancada y altura mínima.** Las dos salen de la misma integración del movimiento del sensor entre dos pies planos consecutivos. Marco global con el eje vertical alineado con la gravedad; la orientación se obtiene integrando el giroscopio y se recalibra con la gravedad en cada pie plano. Condiciones de contorno en cada pie plano: velocidad nula y altura nula (nivel del suelo); la deriva se corrige de forma lineal entre los dos pies planos. La longitud es el avance horizontal, directamente en metros, sin constante que calibrar ni distancia o talla de referencia. La altura (`altura_minima_cm`) es la distancia mínima pie-suelo: el mínimo de la trayectoria vertical durante el balanceo, entre sus dos máximos; si el balanceo no tiene dos máximos, la zancada no aporta altura mínima. Es la altura del sensor respecto a su posición con el pie plano, no la de la puntera. Referencia: Hamacher D, Hamacher D, Taylor WR, Singh NB, Schega L. *Towards clinical application: repetitive sensor position re-calibration for improved reliability of gait parameters.* Gait Posture. 2014;39(4):1146-8. doi:10.1016/j.gaitpost.2014.01.020.
- **6MWT completo y decaimiento.** Un 6MWT es completo si la ventana del registro dura 360 ± 5 s y hay datos válidos en al menos 5 de sus 6 minutos (`minutos_validos`, `ensayo_6mwt_completo`). El decaimiento compara la cadencia del primer minuto con la del último y solo se calcula si el ensayo es completo y ambos minutos son válidos; si no, queda vacío con el motivo (por ejemplo, `6MWT incompleto: 4 minutos válidos`). Las demás métricas del ensayo se informan igualmente, con su cobertura.
- **Sin valor en lugar de cero.** Una métrica que no puede calcularse queda vacía, con su `estado_*` y `motivo_*` en la hoja `Ensayos`, y no entra en la media del paciente.
- **Comprobación entre pies.** Sin eventos anotados, la hoja `Asimetria` compara el número de zancadas de los dos pies (`diferencia_zancadas`); `zancadas_coherentes` es cierto si difieren como mucho en una por tramo continuo.
- **Hoja `Zancadas`.** Una fila por zancada integrada, con su longitud, altura mínima y máxima, o la causa de exclusión.
- **Validación.** No hay eventos anotados ni medidas de otro dispositivo. La comprobación disponible es visual, en Grafana, más las comprobaciones internas (coherencia entre pies y tiempo de zancada dentro de la banda).

Siguen sin validar con una referencia, y así se marcan: polaridad de la presión, valores con los que se delimita un giro, longitud de zancada y altura mínima. Las preguntas abiertas están en `DUDAS.pdf`.

### Migración del HDF5 antiguo

```bash
python scripts/migrar_hdf5_antiguo.py --dry-run
python scripts/migrar_hdf5_antiguo.py
```

Pasa al almacén actual las series del archivo único anterior que coinciden con el registro en paciente, test y ventana temporal, aplicando la misma normalización que una descarga. No consulta InfluxDB ni modifica el archivo antiguo. Las series rechazadas se listan y hay que descargarlas.

## La hora de los datos

- **Registro (`tests.csv`).** Guarda instantes reales con zona horaria: `+01` en invierno y `+02` en verano. `19:24+01` son las 18:24 UTC reales.
- **InfluxDB.** Guarda la hora de reloj de Madrid etiquetada como UTC. Desde noviembre de 2025 todo lo verificable es coherente con eso.
- **Qué hace el pipeline.** `msTools.timeutils.experiment_wallclock` descarta el desfase del registro y se queda con la hora de reloj (`19:24`), que es la que se busca en InfluxDB. No convierte a UTC. Si un evento anterior a noviembre de 2025 vuelve sin datos, se avisa en nivel 1 (`INFO_EMPTY_BEFORE_COHERENT_DATE`).

## Paquete heredado (`pkgs/`), fuera del alcance

`msfeat-pipeline` y `msfeat-graficas` solo usan de `pkgs/` la parte compartida: la configuración (`msConfig`), los mensajes (`msTools.i18n`) y las utilidades de tiempo y de Flux (`msTools.timeutils`, `msTools.flux`); un test lo comprueba. La API, `DataManager`, `MovementDetector`, `CodeIDProcessor`, `ms_monitoring`, `msgait.service`, `consulta_influxdb.py` e `InfluxDBms/` quedan fuera del TFG y se conservan sin mantener. La sección `movement` ya no está en la plantilla de configuración: solo la usa el detector heredado, que tiene su propio `pkgs/.config.yaml`.

Lo que se corrigió en ese código antes de fijar el alcance:

- **Credenciales:** ninguna está escrita en el código. La API exige `MSGAIT_API_KEY` en el entorno y, si falta, rechaza las peticiones protegidas.
- **Flux:** todos los valores se insertan con `msTools.flux.flux_string`.
- **PostgreSQL:** la conexión se abre al usarla; las escrituras validan cada fila, usan identificadores entre comillas, no modifican el DataFrame recibido y propagan los errores.
- **Sin implementar:** `GaitClassifier` y `TrajectoryAnalyzer` lanzan `NotImplementedError`.

Solo está probado con simulaciones (`tests/test_access_lr.py`); no hay base PostgreSQL de pruebas. Limitación conocida, deducida del código y no comprobada con datos: las tablas heredadas se rellenan con la hora de reloj de InfluxDB etiquetada como UTC, pero `ensure_utc()` convierte las consultas a UTC real, así que puede haber un desfase de 1-2 h entre lo que se guarda y lo que se busca.

## Gráficas

```bash
msfeat-graficas                                         # lee paths.output_dataset
msfeat-graficas --data otro.xlsx --out carpeta --verbose 2
```

Lee el Excel del pipeline (necesita las hojas `Datos_Pacientes`, `Ensayos` y `Procedencia`) y escribe en `resultados_graficos/`. Todas las figuras son exploratorias y llevan al pie el archivo de origen, su fecha, el commit, la huella de la configuración y el tamaño de muestra.

- **Boxplots y asimetría:** un valor por paciente, con n >= 3. El eje de la asimetría cubre todo el rango del índice (0-200 %) y se anota cuántos pacientes no tienen asimetría calculable. La línea del 10 % es una referencia sin significado clínico acreditado.
- **Correlaciones:** exploratorias. Spearman por pares, cada par con sus propios casos completos y n >= 10; valores p ajustados por FDR (Benjamini-Hochberg, 0,05). Los pares relacionados por construcción no se calculan ni entran en el ajuste: apoyo y balanceo del mismo pie, zancadas y cadencia de zancada del mismo pie, e índice de simetría con la cadencia de cada pie. Junto a cada matriz se guarda `correlaciones_<test>.csv` con N, rho, p, p ajustado y la causa de exclusión de cada par.
- **Dispersión:** cadencia frente a fase de apoyo, un punto por ensayo y pie, un color por pie; los puntos huecos tienen alguna de las dos métricas en estado WARNING.

## Benchmark

```bash
python benchmarks/benchmark_pipeline.py                          # sintético, sin base de datos
python benchmarks/benchmark_pipeline.py --server --ids 68,69     # eventos reales contra InfluxDB
```

Mide por etapa (consulta, normalización, escritura, lectura y métricas) el tiempo y el pico de memoria de cada evento, y guarda las medidas en `benchmarks/resultados_<modo>.csv`. Escribe en un directorio temporal, nunca en el almacén configurado. El procesamiento es secuencial y lee cada evento completo en memoria.

## Verificación offline

```bash
poetry run pytest -q tests
```

Se comprueban precedencia, tipos, secretos literales, rutas, ausencia de variables, YAML inválido y opciones transmitidas a los clientes simulados. La variable desactiva plugins ajenos al proyecto: el plugin Dash global del entorno revisado falla antes de ejecutar pruebas. No se consultan bases de datos ni se exportan resultados reales.

## Pacientes excluidos, reintentos y eventos detectados

- **`registry.exclude_codeids`.** Lista de pacientes cuyos eventos no se procesan (por ejemplo `[AMIR-48]`, pruebas hechas sin los calcetines puestos). No se consultan ni se borran del registro: aparecen en `Estado_Eventos` con estado `EXCLUDED`.
- **Reintentos.** `influxdb.retry_waits_s` solo se aplica a errores de red (conexión rechazada o cortada, tiempo agotado, respuestas 5xx). Un 401 o una consulta mal formada no se reintentan. `influxdb.connect_timeout` (ms) limita el tiempo para conectar y es independiente de `timeout`, el de lectura.
- **Eventos detectados.** Junto al almacén de señales se guarda, por evento, un HDF5 con los contactos de talón, las zancadas y los giros detectados, y con la versión del pipeline y la huella de `processing`. Sirve para reconstruir de dónde sale cada cifra del Excel.
- **Carpeta de trabajo.** Las rutas relativas y la configuración por defecto se buscan en la carpeta del repositorio. Si el paquete se instala fuera de ella, se usa la carpeta actual o la que indique `MSFEAT_HOME`.
