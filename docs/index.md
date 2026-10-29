# 2026_msFeat_LR

Documentación del código del Trabajo Fin de Grado *Identificación de atributos
cuantificables para monitorización de alteraciones en la marcha* (Lucía Rosado
Rivas, Universidad Politécnica de Madrid, 2026). El código está en
<https://github.com/MultipleSclerosisMonitoring/2026_msFeat_LR>, bajo licencia
AGPL-3.0-or-later.

La aplicación descarga de InfluxDB las señales de marcha de unos calcetines con
sensores, las guarda en HDF5, calcula por ensayo y pie las métricas de la marcha
con su estado de confianza y las reúne en un libro de Excel (*DataSet Maestro*).
Esta documentación no incluye datos de pacientes: para ejecutar el pipeline
hacen falta el registro de ensayos y el acceso a la base de datos, que no se
distribuyen con el código.

```{toctree}
:maxdepth: 2
:caption: Contenido

proyecto
configuracion
modulo
api
```
