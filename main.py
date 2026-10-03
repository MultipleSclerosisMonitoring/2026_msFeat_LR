"""Extract curated test windows using the shared YAML/.env configuration."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'pkgs'))
from msConfig import load_config, ConfigError


def main():
    parser = argparse.ArgumentParser(description='Extracción InfluxDB → HDF5')
    parser.add_argument('--config', default=None, help='YAML; por defecto config/config.yaml')
    parser.add_argument('--csv', default=None, help='Sustituye paths.registry_csv')
    parser.add_argument('--test-type', default=None, help='Sustituye batch.test_type')
    parser.add_argument('--output-folder', default=None, help='Sustituye salida y h5_path')
    parser.add_argument('--check-config', action='store_true', help='Validar YAML/.env sin acceder a bases de datos')
    args = parser.parse_args()
    if args.check_config:
        load_config(args.config)
        print('Configuración válida; no se han realizado conexiones.')
        return
    from gait_analysis.extractor import GaitDataExtractor
    extractor = GaitDataExtractor(args.config, args.output_folder)
    try:
        extractor.run_batch_extraction(args.csv, args.test_type)
    finally:
        extractor.close()


if __name__ == '__main__':
    try:
        main()
    except ConfigError as error:
        sys.exit(str(error))
