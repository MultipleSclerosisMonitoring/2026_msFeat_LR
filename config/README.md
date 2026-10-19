# Configuración única: YAML + .env

El cargador `msConfig.load_config` lee la configuración del pipeline y de las gráficas. Solo lee configuración; no abre conexiones ni modifica `os.environ`.

## Preparación

Para una instalación nueva desde la raíz:

```bash
pipx install poetry                 # una sola vez en el equipo
poetry install --all-extras         # crea .venv/ con las versiones de poetry.lock
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

En PowerShell, activar con `.\.venv\Scripts\Activate.ps1` y copiar con `Copy-Item`. Completar `.env` con los datos de conexión. En este espacio de trabajo ya existen `config/config.yaml` y `.env`; no volver a copiar las plantillas sobre esos archivos.

`.env` contiene URL, organización, bucket y token InfluxDB, y host, usuario, contraseña y base PostgreSQL. Es local, ignorado por Git y creado con permisos 0600 en esta migración. Los YAML de compatibilidad ahora usan `${INFLUXDB_TOKEN}`, `${POSTGRES_PASSWORD}`, etc.; ningún cargador conserva una segunda implementación de lectura YAML.

Se conserva el archivo privado ignorado `InfluxDBms/.config_db.yaml`, ahora también con referencias a variables. Los YAML ya seguidos por Git se sustituyen por referencias, no se eliminan. Esto retira los valores del árbol actual, pero no del historial Git: si eran secretos válidos publicados, su rotación y limpieza histórica son acciones independientes.

## Parámetros y rutas

`config/config.example.yaml` documenta el esquema completo y conserva los parámetros de movimiento de la configuración de referencia:

- `influxdb`: conexión, `measurement`, `timeout` en milisegundos, `verify_ssl` y `ssl_ca_cert`. El certificado TLS se verifica por defecto; `ssl_ca_cert` es opcional, para una CA propia. Si se desactiva con `verify_ssl: false` (o el nombre antiguo `verify`), cada ejecución lo avisa en nivel 0 y los avisos de urllib3 no se silencian. La URL debe usar el nombre del servidor que figura en su certificado, no la IP.
- `postgresql`: sección del código heredado, ya retirado; el pipeline no la usa. Si existe, el cargador comprueba que sus variables estén completas.
- `paths.registry_csv`, `paths.output_folder`, `paths.h5_path`, `paths.output_dataset`: entrada y salidas del pipeline. Si falta alguna, el cargador aplica un único valor por defecto relativo a la raíz del repositorio (`tests.csv`, `data/raw`, `data/raw/gait_study_data.h5`, `resultados_excel/DatasetMaestro.xlsx`), nunca al directorio de ejecución. Si una ejecución termina sin datos, el Excel anterior se aparta en la misma carpeta como `DatasetMaestro.anterior-AAAAMMDD-HHMMSS.xlsx` y la orden termina con error.
- `processing.fs`: frecuencia nominal (50 Hz por defecto). Solo sirve para señalar las series cuya frecuencia medida es distinta; todas las métricas usan la frecuencia medida en cada serie. Debe ser finita y mayor que 10 Hz.
- `batch.test_type`: selección para extracción HDF5; `project.language`: idioma del pipeline directo.

Todas las entradas de `paths` se resuelven respecto al directorio del YAML. Por eso la plantilla en `config/` usa `../tests.csv` y `../data/raw`. Son independientes del directorio de ejecución. Las rutas pasadas como argumentos CLI se interpretan respecto al directorio de ejecución.

## Selección y precedencia

1. Se usa el YAML indicado con `--config`; sin argumento, `config/config.yaml`. Si no existe, la orden termina indicando que se cree desde la plantilla.
2. Una ruta explícita se busca primero respecto al directorio de ejecución, después respecto a la raíz. Se siguen aceptando `config_db.yaml` y `.config_db.yaml` como nombres en `InfluxDBms/`.
3. Configuraciones del repositorio usan el `.env` de la raíz, sin búsqueda en directorios padre. Un YAML externo usa el `.env` de su propio directorio. La API Python acepta `dotenv_path=...` para selección explícita.
4. Variables del proceso tienen prioridad sobre `.env`. El YAML solicita variables mediante `${NOMBRE}`; una variable definida en el proceso no sustituye un literal YAML sin marcador. Se expanden mapas/listas y marcadores dentro de cadenas.
5. No se hace interpolación dentro de `.env`: se conservan literalmente `$`, `#` y otros caracteres de los secretos correctamente entrecomillados. No se expanden variables en cascada.
6. CLI tiene prioridad sobre las rutas/tipo de test YAML que expone cada comando. Después se usan valores por defecto documentados: timeout 30000 ms, measurement Gait, puerto 5432, fs nominal 50 Hz, test 6MWT e idioma es.

Se rechazan variables ausentes/vacías, YAML no estructurado, conexiones incompletas, URL HTTP(S) inválida, booleanos distintos de true/false, timeout/puerto inválidos y frecuencia incompatible. Los errores no incluyen los valores secretos. No imprimir el diccionario resuelto: contiene credenciales.

## Ejecución

`msfeat-pipeline` es el único comando: extrae de InfluxDB, guarda las señales, calcula las métricas y publica el Excel. Se ejecuta en el entorno de Poetry (`poetry run msfeat-pipeline ...`, o tras `poetry shell`); `python pipeline_test.py` es un lanzador de la misma orden.

Comprobar configuración sin conectar:

```bash
msfeat-pipeline --check-config
msfeat-pipeline --check-config --config otra/config.yaml
```

Procesar el registro configurado:

```bash
msfeat-pipeline
msfeat-pipeline --config config/config.yaml --csv tests.csv --test-type 6MWT
```

`--check-config` verifica la configuración, incluidos todos los parámetros de `processing`; no autentica ni demuestra conectividad. `msfeat-graficas` (figuras a partir del Excel) es auxiliar y no forma parte del pipeline.

## Selección, reanudación y niveles de información

- `--ids 58,60`: procesa solo esos `id` del registro.
- `--test-type 6MWT`: procesa solo ese tipo de test; sin la opción, todos.
- `--missing-only`: usa el manifest para no volver a consultar los eventos completados; sus métricas se recalculan leyendo el almacén. Antes de reutilizar un evento se comprueba que el paciente, la prueba y las fechas guardadas coinciden con el registro actual; si no, se vuelve a consultar (`WARN_STORED_IDENTITY`).
- `--workers N`: N consultas simultáneas a InfluxDB (por defecto 1). La escritura y el análisis siguen en un único hilo y en el orden del registro.
- Reintentos: si una consulta falla por conexión se reintenta tras las esperas de `influxdb.retry_waits_s` (5, 15 y 45 s); el número de intentos queda en el manifest y en `Estado_Eventos`.
- `--stored-only`: recalcula solo los eventos ya guardados, sin ninguna consulta a InfluxDB. Es el modo para probar cambios de algoritmo o de parámetros. Un evento cuya identidad guardada no coincide con el registro queda `IDENTITY_MISMATCH`, sin métricas.
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

Por evento y pie: si hay más de un dispositivo (se cambió el calcetín al agotarse la batería), sus tramos se unen por tiempo, porque en cada instante hay un único registro por pie; si dos dispositivos se solapan, en el tramo común se conserva el que tiene más muestras en él, con aviso. Las filas idénticas se eliminan. Las que repiten `_time` con valores distintos se conservan las dos: la marca es la de recepción y son muestras reales (con `time_grid: uniform` se recolocan a paso constante). Todo queda en el manifest. InfluxDB no tiene etiqueta de sesión (etiquetas: `CodeID`, `DeviceName`, `Foot`, `app`, `lat`, `lng`, `mac`, `type`), así que la serie física se identifica por evento y pie.

Por ensayo: `Datos_Ensayos` tiene una fila por ensayo con su fecha y, junto a cada grupo de métricas, su estado de confianza (`{test}_{pie}_estado_cadencia`, `_estado_fases`, `_estado_zancada`, `_estado_altura`, `_estado_decaimiento`: OK o WARNING). Por paciente: ningún ensayo se descarta y los valores en WARNING entran siempre en medias y gráficos; `{test}_ensayos_warning` dice cuántos ensayos del paciente lo están, y `{test}_{pie}_estado_{grupo}_n_warning`, cuántos en cada grupo de métricas (en los diagramas de caja, punto hueco). La asimetría bilateral se calcula solo dentro de cada evento; después, cada métrica del paciente es la media de todos sus ensayos con valor de ese test, y la asimetría la media de sus ensayos bilaterales (en el T25FW, de sus sesiones bilaterales).

El Excel se escribe en un temporal, se relee y solo entonces sustituye al anterior. Es el único informe del proyecto y su primera hoja, `Leeme`, declara el alcance y el contenido de cada hoja: `Datos_Pacientes` (una fila por paciente), `Agregacion_Pacientes`, `Ensayos` (una fila por evento y pie, con estado, motivo, cobertura y filtro de cada métrica), `Datos_Ensayos` (una fila por ensayo), `Calidad_Series` (resultado de cada filtro de calidad por serie), `Asimetria` (con `bilateral_available`), `Zancadas` (una fila por zancada integrada), `Estado_Eventos` (con el número de métricas con valor de cada evento), `Parametros`, `Procedencia` (commit de git, huellas de la configuración y del registro, fecha y recuentos), `Estadisticas_Ensayos` y `Estadisticas_Globales`. Las estadísticas usan una lista explícita de métricas e informan N total, N con valor y N sin valor además de media, mediana y desviación típica, entre ensayos y entre pacientes.

### Procesado de señales

Los parámetros están en la sección `processing` del YAML (plantilla comentada en `config/config.example.yaml`). Se validan al arrancar (tipos, signos, cortes por debajo de Nyquist, límites que no se contradigan) y los valores usados se exportan en la hoja `Parametros`.

- **Tiempo real.** Cada serie exige `_time`. Las marcas de `_time` son de recepción en el móvil y llegan en ráfagas, mientras que el sensor muestrea a ritmo constante. Por eso la frecuencia efectiva se mide como muestras por segundo de señal continua (no depende de las ráfagas, que se informan como `irregularidad_tiempo`) y, con `time_grid: uniform`, las muestras de cada tramo continuo se recolocan a paso constante: no se interpola ni se añaden o quitan filas, solo se corrige el instante de cada muestra. `processing.fs` (50 Hz) es la nominal; como la real depende del sensor, las series distintas se señalan en `fs_desviada` y se resumen en nivel 1 (`INFO_FS_RATES`).
- **Recepción retrasada.** Antes de buscar huecos, en cada intervalo de más de 100 ms entre filas se cuentan las muestras recibidas en el intervalo más los 200 ms siguientes y se comparan con las que corresponden por duración. Si llega el 90 % o más, el móvil recibió tarde un bloque completo: no hay hueco y las marcas se reparten de forma uniforme (`intervalos_recuperados`). Si llega menos, es un hueco real y las zancadas que lo atraviesan se excluyen.
- **Huecos, por grupo de sensores.** Hay dos grupos: IMU (`Ax` a `Gz`) y presión (`S0` a `S2`). Una fila aporta un valor nuevo a un grupo si cambia alguno de sus canales. El relleno nunca cubre más de `max_interpolation_s` (100 ms) sin valor nuevo: hasta ese umbral las filas sin valor nuevo se interpolan entre los valores vecinos y, si no hay valor posterior en el tramo, repiten el último (filas retenidas). Si pasa más tiempo, esas filas quedan sin valor y forman un hueco del grupo, que corta la serie para ese grupo, aunque no lleguen a `max_gap_s`: en la práctica, el hueco de un grupo lo marca `max_interpolation_s`. `max_gap_s` se aplica a las marcas de tiempo de las filas: si dos filas seguidas se separan más, la serie se corta en tramos (salvo recepción retrasada). Con los valores por defecto los dos umbrales valen 100 ms. Lo observado y lo rellenado se informan por separado en `Calidad_Series` y `Ensayos`: `cobertura_observada_imu` y `cobertura_observada_presion` (fracción de filas con valor nuevo), `cobertura_imu` y `cobertura_presion` (fracción utilizable tras rellenar), `muestras_interpoladas`, `muestras_retenidas` y `muestras_repetidas_reposo` (IMU repetida con el pie quieto, que se acepta como medida). Nada se sustituye por cero. En la presión la regla se aplica siempre y se exige estar fuera de huecos al menos `min_coverage` (80 %) del tiempo.
- **Calidad de la IMU.** Se juzga con la presión, que es independiente del giroscopio. La presión está activa si, en una ventana de 1 s centrada en la muestra, algún sensor varía al menos el 25 % de su rango (p5-p95) y al menos 20 unidades. Con la presión inactiva (pie quieto), la IMU repite el mismo valor y eso es una medida válida, no un hueco: la regla de los 100 ms se aplica solo con la presión activa. La IMU de una serie es válida si, con la presión activa, al menos `min_coverage` (80 %) del tiempo queda fuera de huecos (`imu_cobertura_presion_activa_prc`): la misma regla y la misma definición de hueco (más de 100 ms sin valor nuevo) que la presión. No se cuentan las filas repetidas (`imu_repeticion_presion_activa_prc` se informa, pero no decide), porque la IMU de casi la mitad de las series envía unos 13,6 valores nuevos por segundo frente a unas 50 o 100 filas (medido con `scripts/medir_imu.py` en 697 series de 59 calcetines; el resto, entre 47 y 98). Hace falta un mínimo de presión activa (`presion_activa_seg`): `imu_min_active_s` (30 s; 6MWT y 2MWT) o el de la prueba en `imu_min_active_s_by_test` (T25FW: 2 s; en los intentos la presión activa cubre casi todo el intento y el 95 % de las series supera 2,9 s); si no, queda como no evaluable. El mínimo de 20 unidades de la presión activa es absoluto y hace que el criterio dependa de la escala de la lectura; se mantiene a propósito y se estudia con `benchmarks/sensibilidad.py`. `imu_estado` vale `valida`, `no_valida` o `no_evaluable`, y en los dos últimos casos no se calculan la longitud de zancada ni la excursión del tobillo, ni se localizan los giros. Con unos 13,6 valores por segundo la IMU puede ser válida y aun así no bastar para una buena estimación de la longitud (limitación declarada).
- **Qué cubre la hipótesis de continuidad.** No hay contador de muestra ni de paquete en los datos. Dentro de un tramo sin huecos se supone que no falta ninguna muestra. Eso cubre las ráfagas, los retrasos de recepción y los huecos que dejan menos del 90 % de las muestras esperadas. No cubre las pérdidas pequeñas que no dejan un intervalo de más de 100 ms ni bajan del 90 %: en ese caso el tiempo del tramo queda ligeramente comprimido.
- **Vertical estimada.** El calcetín no siempre queda igual de orientado. En cada serie se estima la dirección de la gravedad con las muestras en reposo y se proyectan sobre ella la aceleración (`Av`, vertical) y el giroscopio (`Wv`, giro en torno a la vertical). `inclinacion_grados` es el ángulo entre esa vertical y el eje `Ay`; si no hay reposo utilizable se usan `Ay` y `Gy` y `vertical_estimada` es falso.
- **Zancadas por contacto de talón.** `S2` es el talón; `S0` y `S1`, el antepié. Lo que se guarda es una tensión que baja con la carga: valor bajo = apoyo. Antes de detectar se mide el ritmo de marcha del talón por ventanas de 20 s: se quita la deriva con un paso alto a 0,3 Hz y se exige un pico (máximo local) de la autocorrelación de al menos 0,3 dentro del periodo de zancada (0,8-2,5 s). Las ventanas sin ritmo se excluyen (`ventanas_con_ritmo_prc`, motivo `sin_ritmo`); si ninguna lo tiene, la serie queda como `presión sin modulación de marcha`. Si ningún tramo dura 5 s, decide el rango mínimo de 40 unidades. El detector (valle más cruce de umbral, adaptado del proyecto TEA) localiza el valle de mitad de balanceo, con profundidad de al menos el 30 % del rango, y toma como contacto el primer instante posterior con el talón cargado. Dos contactos no pueden estar a menos de 0,8 s y una zancada vale si dura entre 0,8 y 2,5 s. No se cambia a otra señal. La cadencia es 60 entre el tiempo medio de las zancadas válidas.
- **Duración plausible (D16).** En T25FW (7,62 m) y TUG (al menos `tug_walk_distance_m`, 6 m) la duración registrada implica una velocidad media. Si supera `max_walking_speed_ms` (2,533 m/s: velocidad máxima media del grupo más rápido de adultos sanos, hombres de 20 a 29 años; Bohannon, Age Ageing 1997;26:15-19), el ensayo queda con `duracion_plausible` falso, el motivo `velocidad_implicita_no_plausible` y sin métricas de marcha. En T25FW tampoco es plausible un intento de más de `t25fw_max_s` (180 s, límite por intento del manual del MSFC, 2001). El TUG no tiene máximo. Nada se borra; una duración no plausible no entra en la media del paciente.
- **Mínimo de zancadas por prueba.** 6MWT: 30 en el ensayo completo y 20 en cada minuto que entra en el decaimiento. T25FW: 4, juntando las zancadas de los intentos de la misma sesión del paciente (intentos separados como mucho `pooling_session_gap_s`, 30 min provisionales, entre el fin de uno y el inicio del siguiente: una sesión de mañana y otra de tarde no se juntan); el resultado es orientativo y aparece en `Datos_Pacientes` (cada intento por separado queda sin cadencia). Su asimetría se calcula sesión a sesión, solo en las sesiones en que los dos pies alcanzan el mínimo, y la del paciente es la media de esos índices: nunca se comparan pies de sesiones distintas (`sesiones_bilaterales` en `Agregacion_Pacientes`). TUG: no se informan cadencia, fases, longitud, excursión ni asimetría; su métrica es la duración. 2MWT: 10. Una prueba sin mínimo definido no informa métricas de marcha. En todos los ensayos se descarta la primera zancada de cada pie (`skip_first_strides`).
- **Giros por ángulo acumulado.** La velocidad de giro en torno a la vertical se suaviza para quitar la oscilación de cada zancada. Un episodio tiene un núcleo por encima de `turn_threshold` y se extiende a ambos lados hasta bajar de `turn_extend_threshold`, para no perder el ángulo del arranque y del final. Es un giro si el ángulo acumulado llega a `turn_min_angle_deg` (90°). Retirar los giros no pega tramos separados. Además del giro se excluyen, por pie, las 3 zancadas anteriores y las 2 posteriores (`turn_margin_before_strides`, `turn_margin_after_strides`, `zancadas_margen_giro`): es la versión en zancadas de la exclusión espacial de Müller et al. (2021). Solo puede aplicarse si la IMU es válida; si no lo es no se sabe dónde están los giros, `giros_excluidos` es falso y la cadencia incluye frenadas y arranques.
- **Fases.** Cada ciclo es una zancada válida. El pie está en el suelo si algún sensor de presión está cargado; el resto del ciclo es balanceo. Un ciclo solo cuenta si su balanceo dura 0,25-0,8 s y ocupa el 20-55 % del ciclo.
- **Longitud de zancada y excursión del tobillo.** Las dos salen de la misma integración del movimiento del sensor entre dos pies planos consecutivos (Hamacher et al., 2014). Marco global con el eje vertical alineado con la gravedad; la orientación se obtiene integrando el giroscopio y se recalibra con la gravedad en cada pie plano. Condiciones de contorno en cada pie plano: velocidad nula y altura nula (nivel del suelo). No hay constantes que calibrar. Que el sensor esté quieto en el pie plano es una hipótesis: en cada uno se mide, en una ventana de `still_window_s` (0,2 s, al menos tres valores nuevos de la IMU), el desvío máximo del módulo de la aceleración respecto a 1 g (`quietud_acc_g`; quieto si no supera `rest_accel_tolerance_g`, 0,1 g: `quieto_acc`), el giro máximo (`quietud_giro_dps`), el giro en el pie plano (`giro_min_apoyo_dps`) y los valores nuevos (`quietud_valores_nuevos`). Se informan en `Zancadas` y se cuentan por serie en `Calidad_Series` (`zancadas_integradas`, `zancadas_quietas_acc`), pero no excluyen ninguna zancada. Medido con `scripts/medir_quietud.py`: en el 6MWT solo entre el 25 y el 30 % de las zancadas cumplen la condición de la aceleración y el giro en el pie plano tiene mediana de 40-58 °/s, así que la hipótesis de quietud no se cumple en la mayoría y se declara como limitación. El módulo va en la caña del calcetín, a la altura del tobillo: la altura integrada es la excursión vertical del tobillo (`excursion_tobillo_cm`), no la distancia mínima punta-suelo (MTC), que queda vacía con su motivo y no se calcula en este trabajo.
- **6MWT completo y decaimiento.** Un 6MWT es completo si la ventana del registro dura 360 ± 5 s y hay datos válidos en al menos 5 de sus 6 minutos (`minutos_validos`, `ensayo_6mwt_completo`). El decaimiento es la pendiente de la recta ajustada a la cadencia de cada minuto (`pendiente_cadencia`, en zancadas por minuto cada minuto), como en Müller et al. (2021); `decaimiento_prc` es la pérdida entre el primer y el último minuto según esa recta. Un minuto aporta cadencia con al menos 20 zancadas válidas, y hacen falta al menos 4 minutos con cadencia (`minutos_con_cadencia`), entre ellos el primero y el último; si no, queda vacío con el motivo.
- **Sin valor en lugar de cero.** Una métrica que no puede calcularse queda vacía, con su `estado_*` y `motivo_*` en la hoja `Ensayos`, y no entra en la media del paciente.
- **Comprobación entre pies.** Sin eventos anotados, la hoja `Asimetria` compara el número de zancadas de los dos pies (`diferencia_zancadas`); `zancadas_coherentes` es cierto si difieren como mucho en una por tramo continuo.
- **Hoja `Zancadas`.** Una fila por zancada integrada, con su longitud y su excursión del tobillo, o la causa de exclusión (`en_giro`, `margen_giro`, `duracion_fuera_de_rango`).
- **Validación.** No hay eventos anotados ni medidas de otro dispositivo. La comprobación disponible es visual, en Grafana, más las comprobaciones internas (coherencia entre pies y tiempo de zancada dentro de la banda).

Siguen sin validar con una referencia, y así se marcan: valores con los que se delimita un giro, correspondencia de cada contacto de talón con un apoyo real, polaridad de la presión (la presión sube al pisar y el valor guardado baja; la lectura es relativa, no absoluta), longitud de zancada y excursión del tobillo. En el T25FW, cuya distancia es de 7,62 m por definición (`processing.t25fw_distance_m`), se avisa cuando la suma de las longitudes de zancada de un intento no es coherente con ella: no debe superarla ni quedarse más de dos zancadas medias por debajo (las parciales del principio y del final), más una por cada zancada que no pudo integrarse. Es un aviso (`WARN_T25FW_DISTANCE`, columnas `t25fw_suma_longitudes_m` y `t25fw_distancia_coherente` en `Ensayos` y `Calidad_Series`); no valida ni calibra la longitud. En los 6MWT no hay distancia anotada. Las preguntas abiertas están en `DUDAS.pdf`. Para revisar ensayos concretos frente a Grafana: `poetry run python scripts/revisar_referencias.py` (ensayos de referencia 68, 261, 167, 89, 108, 291 y 232; `--detalle archivo.csv` escribe el instante de cada evento detectado).

### Migración del HDF5 antiguo

```bash
poetry run python scripts/migrar_hdf5_antiguo.py --dry-run
poetry run python scripts/migrar_hdf5_antiguo.py
```

Pasa al almacén actual las series del archivo único anterior que coinciden con el registro en paciente, test y ventana temporal, aplicando la misma normalización que una descarga. No consulta InfluxDB ni modifica el archivo antiguo. Las series rechazadas se listan y hay que descargarlas.

## La hora de los datos

- **Registro (`tests.csv`).** Guarda instantes reales con zona horaria: `+01` en invierno y `+02` en verano. `19:24+01` son las 18:24 UTC reales.
- **InfluxDB.** Guarda la hora de reloj de Madrid etiquetada como UTC. Desde noviembre de 2025 todo lo verificable es coherente con eso.
- **Qué hace el pipeline.** Depende de `registry.influx_time`, que admite un criterio único o una lista de periodos (`desde`, `criterio`), de modo que cada evento usa el del periodo de su fecha. Con `civil`, `msTools.timeutils.experiment_wallclock` descarta el desfase del registro y se queda con la hora de reloj (`19:24`). Con `utc` se busca el instante real convertido a UTC (`18:24` en invierno; dos horas menos en verano). Por defecto: `utc` desde el 1 de noviembre de 2025 y `civil` antes. Si un evento anterior a noviembre de 2025 vuelve sin datos, se avisa en nivel 1 (`INFO_EMPTY_BEFORE_COHERENT_DATE`).
- **Por qué (D14).** Una comprobación con datos reales (`scripts/comprobar_hora.py`, solo agregados por segundo del sensor del talón) respalda `utc`: en 21 6MWT al azar, la actividad de marcha llena la ventana UTC y no la civil, y el desfase que mejor encaja sigue al cambio de hora (60 min en invierno, 120 en verano). Todos los eventos válidos del registro son de noviembre de 2025 en adelante; para fechas anteriores no hay eventos con los que comprobarlo y se conserva `civil` (R27).
- **Cambiar de criterio obliga a volver a descargar.** Cada evento guarda con sus señales el criterio con el que se descargó (`hora_influx`; lo anterior a esta opción, `civil`). Si no coincide con el de la configuración, `--missing-only` lo vuelve a consultar y `--stored-only` lo deja como `IDENTITY_MISMATCH`. El criterio usado figura en la hoja `Procedencia` (`criterio_hora_influx`).

## Paquete heredado retirado

`msfeat-pipeline` y `msfeat-graficas` solo usan de `pkgs/` la configuración (`msConfig`), los mensajes (`msTools.i18n`) y las utilidades de tiempo y de Flux (`msTools.timeutils`, `msTools.flux`); un test lo comprueba. El resto del paquete heredado (la API, `DataManager`, `MovementDetector`, `CodeIDProcessor`, `ms_monitoring`, `msgait.service`, su documentación, su `.config.yaml` y sus dependencias) se retiró del repositorio el 18/10/2026, porque declararlo fuera del alcance no resolvía sus fallos. Sigue en el historial de Git y en el repositorio original (DPC_2024).

## Gráficas

```bash
msfeat-graficas                                         # lee paths.output_dataset
msfeat-graficas --data otro.xlsx --out carpeta --verbose 2
msfeat-graficas --out carpeta --sin-procedencia         # figuras para la memoria, sin el pie de procedencia
```

Lee el Excel del pipeline (necesita las hojas `Datos_Pacientes`, `Ensayos` y `Procedencia`) y escribe en `resultados_graficos/`. Todas las figuras son exploratorias y llevan al pie el archivo de origen, su fecha, el commit, la huella de la configuración y el tamaño de muestra. Con `--sin-procedencia` se generan sin ese pie, para la memoria del TFG, donde la procedencia y el tamaño de muestra van en el texto y en el pie de cada figura.

- **Boxplots y asimetría:** un valor por paciente, con n >= 3. El eje de la asimetría cubre todo el rango del índice (0-200 %) y se anota cuántos pacientes no tienen asimetría calculable. La línea del 10 % es una referencia sin significado clínico acreditado.
- **Correlaciones:** exploratorias. Spearman por pares, cada par con sus propios casos completos y n >= 10; valores p ajustados por FDR (Benjamini-Hochberg, 0,05). Los pares relacionados por construcción no se calculan ni entran en el ajuste: apoyo y balanceo del mismo pie, zancadas y cadencia de zancada del mismo pie, e índice de simetría con la cadencia de cada pie. Junto a cada matriz se guarda `correlaciones_<test>.csv` con N, rho, p, p ajustado y la causa de exclusión de cada par.
- **Dispersión:** cadencia frente a fase de apoyo, un punto por ensayo y pie, un color por pie; los puntos huecos tienen alguna de las dos métricas en estado WARNING.

## Benchmark

```bash
python benchmarks/benchmark_pipeline.py                          # sintético, sin base de datos
python benchmarks/benchmark_pipeline.py --server --ids 68,69     # eventos reales contra InfluxDB
```

Mide por etapa (consulta, normalización, escritura, lectura y métricas) el tiempo y el pico de memoria de cada evento, y guarda las medidas en `benchmarks/resultados_<modo>.csv`. Escribe en un directorio temporal, nunca en el almacén configurado. El procesamiento es secuencial y lee cada evento completo en memoria.

## Comprobaciones con datos reales

```bash
python scripts/comprobar_hora.py --prueba 6MWT --n-por-estrato 3   # criterio horario (D14), solo agregados por segundo
python scripts/medir_imu.py                                       # tasa real de la IMU y presión activa por serie (D03)
python benchmarks/sensibilidad.py --prueba 6MWT                   # sensibilidad de la cadencia y las fases (D17)
```

Ninguna modifica el almacén ni la base de datos. El detalle se guarda en `data/` (fuera de Git) sin el código del paciente, y por pantalla solo salen recuentos agregados. La sensibilidad solo describe los datos si las señales se descargaron con el criterio horario correcto.

## Verificación offline

```bash
poetry run pytest -q tests
```

Se comprueban precedencia, tipos, secretos literales, rutas, ausencia de variables, YAML inválido y opciones transmitidas a los clientes simulados. La variable desactiva plugins ajenos al proyecto: el plugin Dash global del entorno revisado falla antes de ejecutar pruebas. No se consultan bases de datos ni se exportan resultados reales.

## Pacientes excluidos, reintentos y eventos detectados

- **`registry.exclude_codeids`.** Lista de pacientes cuyos eventos no se procesan (por ejemplo `[AMIR-48]`, pruebas hechas sin los calcetines puestos). No se consultan ni se borran del registro: aparecen en `Estado_Eventos` con estado `EXCLUDED`.
- **Reintentos.** `influxdb.retry_waits_s` solo se aplica a errores de red (conexión rechazada o cortada, tiempo agotado, respuestas 5xx). Un 401 o una consulta mal formada no se reintentan. `influxdb.connect_timeout` (ms) limita el tiempo para conectar y es independiente de `timeout`, el de lectura.
- **Eventos detectados.** En la carpeta de resultados (`paths.results_folder`, por defecto `data/results/`, separada de los datos en bruto) se guarda un HDF5 por ensayo (`eventos/p_{paciente}/{test}/trial_{id}.h5`) con una tabla por pie (`{pie}/eventos`): contactos de talón, despegues, giros y tramos excluidos con su motivo (`hueco_presion`, `giro`, `margen_giro`, `sin_ritmo`, `duracion_fuera_de_banda`, `arranque`), más las zancadas integradas (`{pie}/zancadas`). Lleva la versión del código, la huella de `processing` y la fecha de la ejecución; cada ejecución sustituye el archivo del ensayo. El Excel sigue siendo solo el resumen.
- **Carpeta de trabajo.** Las rutas relativas y la configuración por defecto se buscan en la carpeta del repositorio. Si el paquete se instala fuera de ella, se usa la carpeta actual o la que indique `MSFEAT_HOME`.
