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

- `influxdb`: conexión, `measurement`, `timeout` en milisegundos y `verify` o `verify_ssl`. El nombre canónico resuelto es `verify_ssl`; si aparecen ambos tiene prioridad `verify_ssl`. Sin opción se verifica TLS; la plantilla conserva la opción de la configuración original para mantener acceso al servidor.
- `postgresql`: conexión y `port`; el puerto ya se transmite a psycopg2. Esta sección se puede eliminar en configuraciones que solo usan InfluxDB. Si existe, sus variables también deben estar completas.
- `paths.registry_csv`, `paths.output_folder`, `paths.h5_path`, `paths.output_dataset`, `paths.output_report`: entrada y salidas de los comandos principales.
- `processing.fs`: frecuencia usada por el pipeline directo en cadencia y decaimiento. Debe ser finita y mayor que 10 Hz por los filtros actuales. No corrige por sí misma el muestreo irregular ni las limitaciones de los algoritmos descritas en la revisión.
- `batch.test_type`: selección para extracción HDF5; `project.language`: idioma del pipeline directo.
- `movement`: parámetros heredados de detección. Su motor mantiene su contrato de frecuencia recibido por el constructor; no utiliza `processing.fs` automáticamente.

Todas las entradas de `paths` se resuelven respecto al directorio del YAML. Por eso la plantilla en `config/` usa `../tests.csv` y `../data/raw`. Son independientes del directorio de ejecución. Las rutas pasadas como argumentos CLI se interpretan respecto al directorio de ejecución. `--output-folder` también sustituye `h5_path`, usando `gait_study_data.h5` dentro de la carpeta elegida.

## Selección y precedencia

1. Se usa el YAML indicado con `--config`; sin argumento, `config/config.yaml` y, si falta, `pkgs/.config.yaml` como compatibilidad.
2. Una ruta explícita se busca primero respecto al directorio de ejecución, después respecto a la raíz. Se siguen aceptando `config_db.yaml` y `.config_db.yaml` como nombres en `InfluxDBms/`.
3. Configuraciones del repositorio usan el `.env` de la raíz, sin búsqueda en directorios padre. Un YAML externo usa el `.env` de su propio directorio. La API Python acepta `dotenv_path=...` para selección explícita.
4. Variables del proceso tienen prioridad sobre `.env`. El YAML solicita variables mediante `${NOMBRE}`; una variable definida en el proceso no sustituye un literal YAML sin marcador. Se expanden mapas/listas y marcadores dentro de cadenas.
5. No se hace interpolación dentro de `.env`: se conservan literalmente `$`, `#` y otros caracteres de los secretos correctamente entrecomillados. No se expanden variables en cascada.
6. CLI tiene prioridad sobre las rutas/tipo de test YAML que expone cada comando. Después se usan valores por defecto documentados: timeout 30000 ms, measurement Gait, puerto 5432, fs 100 en el pipeline directo, test 6MWT e idioma es.

Se rechazan variables ausentes/vacías, YAML no estructurado, conexiones incompletas, URL HTTP(S) inválida, booleanos distintos de true/false, timeout/puerto inválidos y frecuencia incompatible. Los errores no incluyen los valores secretos. No imprimir el diccionario resuelto: contiene credenciales.

## Ejecución

Comprobar configuración sin conectar ni cargar dependencias científicas:

```bash
python main.py --check-config
python main.py --check-config --config pkgs/.config.yaml
```

Extraer el tipo de test configurado hacia el HDF5 configurado:

```bash
python main.py
python main.py --config config/config.yaml --csv tests.csv --test-type 6MWT
```

Pipeline directo y análisis desde HDF5:

```bash
python pipeline_test.py --config config/config.yaml
python generar_informe_clinico.py --config config/config.yaml
```

Estos comandos de procesamiento consultan datos o generan resultados; `--check-config` solo verifica configuración, no autentica ni demuestra conectividad. Los scripts exploratorios `analisis_*`, `graficas.py`, `process_gait_signals.py` y el batch histórico conservan sus rutas/contratos: no son interfaces oficiales para elegir configuración. Los defectos de cálculo y trazabilidad del informe previo siguen pendientes.

Los comandos de `pkgs` siguen aceptando `-c config/config.yaml`. Para usarlos se requiere también su entorno de dependencias (`python -m pip install ./pkgs`); el proyecto raíz no instala todos los subsistemas heredados de PostgreSQL/API. Los archivos raíz de ejecución están pensados para ejecutarse desde este checkout; no constituyen entrypoints instalados.

## Verificación offline

```bash
python -m pip install pytest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_config.py
```

Se comprueban precedencia, tipos, secretos literales, rutas, ausencia de variables, YAML inválido y opciones transmitidas a los clientes simulados. La variable desactiva plugins ajenos al proyecto: el plugin Dash global del entorno revisado falla antes de ejecutar pruebas. No se consultan bases de datos ni se exportan resultados reales.
