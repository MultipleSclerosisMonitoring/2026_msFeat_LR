# Configuración única: YAML + .env

El cargador `msConfig.load_config` es compartido por el extractor, el pipeline directo, `cInfluxDB` y `DataManager` (incluidos los comandos y la API de `pkgs`). Solo lee configuración; no abre conexiones ni modifica `os.environ`.

## Preparación

Para una instalación nueva desde la raíz:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
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

`pipeline_test.py` es el único comando: extrae de InfluxDB, guarda las señales, calcula las métricas y publica el Excel.

Comprobar configuración sin conectar:

```bash
python pipeline_test.py --check-config
python pipeline_test.py --check-config --config pkgs/.config.yaml
```

Procesar el registro configurado:

```bash
python pipeline_test.py
python pipeline_test.py --config config/config.yaml --csv tests.csv --test-type 6MWT
```

`--check-config` solo verifica configuración, no autentica ni demuestra conectividad. Los scripts `consulta_influxdb.py` y `graficas_tfg.py` son auxiliares (exploración manual y figuras a partir del Excel) y no forman parte del pipeline.

Los comandos de `pkgs` siguen aceptando `-c config/config.yaml`. Para usarlos se requiere también su entorno de dependencias (`python -m pip install ./pkgs`); el proyecto raíz no instala todos los subsistemas heredados de PostgreSQL/API.

## Selección, reanudación y niveles de información

- `--ids 58,60`: procesa solo esos `id` del registro.
- `--test-type 6MWT`: procesa solo ese tipo de test; sin la opción, todos.
- `--missing-only`: usa el manifest para no volver a consultar los eventos completados; sus métricas se recalculan leyendo el almacén.
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

- **Tiempo real.** Cada serie exige `_time`. La frecuencia efectiva es el número de muestras por segundo de señal continua (robusta frente a marcas de tiempo en ráfagas, que se informan como `irregularidad_tiempo`) y es la que se usa en filtros y ventanas; `processing.fs` es solo la nominal y, si la medida se aparta, se avisa una vez al final (`WARN_FS_MISMATCH`). Duraciones y cadencias salen de los tiempos, no del número de muestras.
- **Huecos y valores ausentes.** Un intervalo entre muestras mayor que `max_gap_s` corta la serie en segmentos. Los valores ausentes o no numéricos solo se interpolan en tramos de hasta `max_interpolation_s`; nunca se sustituyen por cero. Un canal con cobertura inferior a `min_coverage` no se usa.
- **Segmentos.** Filtros, detección de picos, ciclos y balanceos se calculan dentro de cada tramo continuo; retirar los giros no pega tramos separados.
- **Giros.** Un episodio por encima de `turn_threshold` solo cuenta como giro si dura al menos `turn_min_duration_s`; los más cortos (el balanceo del pie supera el umbral) se ignoran y se cuentan en `n_giros_descartados`.
- **Sin valor en lugar de cero.** Una métrica que no puede calcularse queda vacía, con su `estado_*` y `motivo_*` en la hoja `Ensayos`, y no entra en la media del paciente.
- **Hoja `Ensayos`.** Añade por pie la frecuencia efectiva, duración real, huecos, cobertura, tiempo y número de giros, modalidad de detección (`accel` o `pressure`) con el recuento de cada una, filtro aplicado, ciclos y balanceos usados y descartados, y la longitud sin recortar.
- **Hoja `Balanceos`.** Una fila por balanceo con su altura o su causa de exclusión.

Siguen sin calibrar o validar, y así se marcan: detector de giros, estimador de longitud (`stride_calibrado` es siempre falso), altura por doble integración y polaridad de la presión. Las preguntas abiertas están en `DUDAS.pdf`.

### Migración del HDF5 antiguo

```bash
python scripts/migrar_hdf5_antiguo.py --dry-run
python scripts/migrar_hdf5_antiguo.py
```

Pasa al almacén actual las series del archivo único anterior que coinciden con el registro en paciente, test y ventana temporal, aplicando la misma normalización que una descarga. No consulta InfluxDB ni modifica el archivo antiguo. Las series rechazadas se listan y hay que descargarlas.

## Paquete heredado (`pkgs/`)

`pipeline_test.py` y `graficas_tfg.py` solo usan de `pkgs/` la configuración (`msConfig`), los mensajes (`msTools.i18n`) y las utilidades de tiempo y de Flux (`msTools.timeutils`, `msTools.flux`); un test lo comprueba. La API, `DataManager`, `MovementDetector` y `CodeIDProcessor` no intervienen en el pipeline.

- **Credenciales:** ninguna está escrita en el código. La API exige `MSGAIT_API_KEY` en el entorno y, si falta, rechaza las peticiones protegidas (no hay clave por defecto).
- **Flux:** todos los valores se insertan con `msTools.flux.flux_string`, que escapa comillas y barras, y los rangos se validan con `flux_range`.
- **PostgreSQL:** la conexión se abre al usarla, no al construir `DataManager`; `postgresql.connect_timeout` es opcional. Las escrituras validan cada fila con el modelo de su tabla, usan identificadores entre comillas, no modifican el DataFrame recibido y propagan los errores tras deshacer la transacción.
- **Sin implementar:** `GaitClassifier` y `TrajectoryAnalyzer` lanzan `NotImplementedError`.

Estas correcciones solo están probadas con simulaciones (`tests/test_access_lr.py`), no contra un PostgreSQL real.

## Qué valida la configuración y cuándo

- **Por tarea.** Cada comando carga solo las conexiones que usa. `pipeline_test.py` necesita InfluxDB; con `--stored-only` o `--audit`, y en `graficas_tfg.py`, no se exige ninguna variable de base de datos. `--check-config` valida todo lo declarado.
- **Parámetros de análisis**, separados de las conexiones: `batch.test_type` (texto no vacío), `project.language` (`es` o `en`), bandas y umbrales de `movement`, y `processing` (ver arriba).
- **Rutas.** El registro de entrada y las salidas (`h5_path`, `output_dataset`) no pueden ser el mismo archivo.

## Idioma de los mensajes

`project.language` admite `es` (por defecto) y `en`. Cambia los mensajes de log, los errores y los textos de las gráficas. No cambia los nombres de hojas y columnas del Excel, la ayuda de la línea de órdenes ni los errores de configuración (se producen antes de conocer el idioma).

Los textos en español del código son los identificadores; `pkgs/locales/en/LC_MESSAGES/msGait.po` tiene su traducción. Tras añadir o cambiar un mensaje:

```bash
python scripts/traducciones.py --list     # mensajes sin traducir
python scripts/traducciones.py            # compila los catálogos .mo (se versionan)
```

## Formatos, unidades y límites de las métricas

Hoja `Ensayos`, una fila por evento y pie. Una celda vacía significa "no calculable"; el motivo está en `motivo_*`.

| Columna | Unidad | Qué es | Límite conocido |
|---|---|---|---|
| `pasos` | recuento | Picos detectados en un pie (zancadas de ese pie) | Detector sin validar con eventos anotados |
| `cadencia` | picos por minuto | `pasos` entre el tiempo de marcha analizado | Banda de plausibilidad 30-200 configurable |
| `apoyo_prc`, `balanceo_prc` | % del ciclo | Media por ciclo completo, con tiempos reales | Depende de la polaridad de la presión (`pressure_stance_is_low`) |
| `decaimiento_prc` | % | Pérdida de cadencia entre el primer y el último minuto (6MWT) | Requiere duración y cobertura mínimas |
| `stride_length_cm` | cm | Estimador empírico a partir de `Ay` | No calibrado (`stride_calibrado` es falso) |
| `step_height_cm` | cm | Doble integración de `Ay` en cada balanceo | No validado |
| `symmetry_index` (hoja `Asimetria`) | % (0-200) | Diferencia de cadencia entre pies sobre su media | Solo con los dos pies del mismo evento |
| `fs_efectiva` | Hz | Muestras por segundo de señal continua | — |
| `duracion_real_seg`, `tiempo_*_seg` | s | Medidos con `_time` | — |

Aceleraciones en g y giroscopio en grados por segundo, según se asume en el código; las horas son la hora civil de Madrid del registro, sin convertir a UTC.

## Gráficas

```bash
python graficas_tfg.py                                  # lee paths.output_dataset
python graficas_tfg.py --data otro.xlsx --out carpeta --verbose 2
```

Lee el Excel del pipeline (necesita las hojas `Datos_Pacientes`, `Ensayos` y `Procedencia`) y escribe en `resultados_graficos/`. Todas las figuras son exploratorias y llevan al pie el archivo de origen, su fecha, el commit, la huella de la configuración y el tamaño de muestra.

- **Boxplots y asimetría:** un valor por paciente, con n >= 3. El eje de la asimetría cubre todo el rango del índice (0-200 %) y se anota cuántos pacientes no tienen asimetría calculable. La línea del 10 % es una referencia sin significado clínico acreditado.
- **Correlaciones:** Spearman por pares, cada par con sus propios casos completos y n >= 10; valores p ajustados por FDR (Benjamini-Hochberg, 0,05). Junto a cada matriz se guarda `correlaciones_<test>.csv` con N, rho, p, p ajustado y la causa de exclusión de cada par.
- **Dispersión:** cadencia frente a fase de apoyo, un punto por ensayo y pie, un color por pie; los puntos huecos tienen alguna de las dos métricas en estado WARNING.

## Benchmark

```bash
python benchmarks/benchmark_pipeline.py                          # sintético, sin base de datos
python benchmarks/benchmark_pipeline.py --server --ids 68,69     # eventos reales contra InfluxDB
```

Mide por etapa (consulta, normalización, escritura, lectura y métricas) el tiempo y el pico de memoria de cada evento, y guarda las medidas en `benchmarks/resultados_<modo>.csv`. Escribe en un directorio temporal, nunca en el almacén configurado. El procesamiento es secuencial y lee cada evento completo en memoria.

## Verificación offline

```bash
python -m pip install pytest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests
```

Se comprueban precedencia, tipos, secretos literales, rutas, ausencia de variables, YAML inválido y opciones transmitidas a los clientes simulados. La variable desactiva plugins ajenos al proyecto: el plugin Dash global del entorno revisado falla antes de ejecutar pruebas. No se consultan bases de datos ni se exportan resultados reales.
