"""Mantenimiento de los catálogos de traducción (sin herramientas externas).

    python scripts/traducciones.py            # compila los .po a .mo
    python scripts/traducciones.py --check    # falla si falta alguna traducción o un .mo está desfasado
    python scripts/traducciones.py --list     # lista los mensajes del código sin traducción al inglés

Los mensajes del pipeline se escriben en español en el código: ese texto es el
identificador (``msgid``). El catálogo ``en`` contiene su traducción; en ``es``
no hace falta entrada porque sin traducción se muestra el identificador.
"""
import argparse
import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "pkgs" / "locales"
DOMAIN = "msGait"
# Archivos cuyos mensajes pasan por la traducción.
SOURCES = (
    "pipeline_test.py", "GaitMetricsCalculator.py", "graficas_tfg.py", "src/gait_analysis",
    "scripts/migrar_hdf5_antiguo.py", "benchmarks/benchmark_pipeline.py",
)


def source_messages() -> dict[str, str]:
    """Reúne los textos marcados con ``_()`` en el código del pipeline.

    Returns:
        dict[str, str]: Archivo y línea de la primera aparición, por mensaje.
    """
    found: dict[str, str] = {}
    for entry in SOURCES:
        path = ROOT / entry
        for file in sorted(path.rglob("*.py")) if path.is_dir() else [path]:
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.setdefault(arg.value, f"{file.relative_to(ROOT).as_posix()}:{node.lineno}")
    return found


def read_po(path: Path) -> dict[str, str]:
    """Lee un archivo ``.po`` (mensajes simples, sin plurales).

    Args:
        path (Path): Archivo ``.po``.

    Returns:
        dict[str, str]: Traducción por identificador; la cabecera tiene identificador vacío.
    """
    entries: dict[str, str] = {}
    key, value, target = None, None, None
    for line in path.read_text(encoding="utf-8").splitlines() + [""]:
        line = line.strip()
        if line.startswith("msgid "):
            if key is not None and value is not None:
                entries[key] = value
            key, value, target = ast.literal_eval(line[6:]), None, "id"
        elif line.startswith("msgstr "):
            value, target = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"') and target == "id":
            key += ast.literal_eval(line)
        elif line.startswith('"') and target == "str":
            value += ast.literal_eval(line)
        elif not line and key is not None and value is not None:
            entries[key] = value
            key, value, target = None, None, None
    return entries


def compile_mo(entries: dict[str, str]) -> bytes:
    """Genera el contenido binario ``.mo`` de GNU gettext.

    Args:
        entries (dict[str, str]): Traducción por identificador.

    Returns:
        bytes: Catálogo compilado.
    """
    items = sorted((key.encode("utf-8"), value.encode("utf-8")) for key, value in entries.items() if value or key == "")
    ids = b"".join(key + b"\0" for key, _value in items)
    strings = b"".join(value + b"\0" for _key, value in items)
    count = len(items)
    key_start = 7 * 4 + 16 * count
    value_start = key_start + len(ids)
    key_table, value_table, key_offset, value_offset = [], [], 0, 0
    for key, value in items:
        key_table += [len(key), key_start + key_offset]
        value_table += [len(value), value_start + value_offset]
        key_offset += len(key) + 1
        value_offset += len(value) + 1
    header = struct.pack("Iiiiiii", 0x950412DE, 0, count, 7 * 4, 7 * 4 + 8 * count, 0, 0)
    return header + struct.pack(f"{2 * count}i", *key_table) + struct.pack(f"{2 * count}i", *value_table) + ids + strings


def main() -> int:
    """Compila o comprueba los catálogos.

    Returns:
        int: 0 si todo es correcto; 1 si ``--check`` encuentra diferencias.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # consolas que no son UTF-8
    parser = argparse.ArgumentParser(description="Compilar y comprobar las traducciones")
    parser.add_argument("--check", action="store_true", help="Comprobar sin escribir")
    parser.add_argument("--list", action="store_true", help="Listar mensajes sin traducción al inglés")
    args = parser.parse_args()

    problems = 0
    messages = source_messages()
    english = read_po(LOCALES / "en" / "LC_MESSAGES" / f"{DOMAIN}.po")
    missing = [message for message in messages if not english.get(message)]
    if args.list or args.check:
        for message in missing:
            print(f"SIN TRADUCCIÓN (en) {messages[message]}: {message!r}")
        problems += len(missing)
    if args.list:
        print(f"{len(messages)} mensajes en el código; {len(missing)} sin traducción al inglés")
        return 0

    for language in sorted(item.name for item in LOCALES.iterdir() if item.is_dir()):
        po = LOCALES / language / "LC_MESSAGES" / f"{DOMAIN}.po"
        mo = po.with_suffix(".mo")
        compiled = compile_mo(read_po(po))
        if args.check:
            if not mo.is_file() or mo.read_bytes() != compiled:
                print(f"DESFASADO {mo.relative_to(ROOT).as_posix()}: ejecute python scripts/traducciones.py")
                problems += 1
        else:
            mo.write_bytes(compiled)
            print(f"Compilado {mo.relative_to(ROOT).as_posix()}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
